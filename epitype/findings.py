import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]
"""Detect, persist, surface, and acknowledge memory-path failures."""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import tempfile
import threading
import time


_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from adapters.codex import hook_trust
from epitype import memsearch, memspec, telemetry


FINDINGS_FILENAME = "_EPITYPE_FINDINGS.md"
DETECTOR_BUDGET_MS = 150
DETECTOR_HEADROOM_MS = 80
ACTIVITY_HOURS = 24
DEFAULT_MISS_STREAK = 8
HOOK_INTERNAL_REASONS = frozenset(("no-index", "timeout", "config", "exception"))
STATUSES = frozenset(("open", "acked", "closed"))
SHIM_REASONS = frozenset(
    (
        "config_missing",
        "config_unreadable",
        "repo_root_missing",
        "repo_root_not_dir",
        "adapter_missing",
        "exception",
    )
)
FAILURE_MODES = {
    "codex-untrusted": "§7",
    "detector-timeout": "§9",
    "fail-open-seen": "§6",
    "host-silent": "§9",
    "hook-internal-failure": "§9",
    "index-unreachable": "§8",
    "recall-miss-streak": "§9",
}
REMEDIES = {
    "codex-untrusted": "In Codex, open /hooks and approve the four Epitype hooks.",
    "detector-timeout": "Run python install/graft.py doctor to retry the bounded detector.",
    "fail-open-seen": "Repair the named shim cause, then run graft doctor --clear-shim-status.",
    "host-silent:claude": "Check Claude hook registration and run graft doctor.",
    "host-silent:codex": "In Codex, open /hooks, approve Epitype, then run graft doctor.",
    "hook-internal-failure": "Repair the recorded hook reason, then run graft doctor and a synthetic recall.",
    "index-unreachable": "Automatically rebuild the stale index; run graft doctor if it remains open.",
    "recall-miss-streak": "Run graft doctor and inspect whether the intended vault is indexed and reachable.",
}
_CODE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9:.-]*$")
_EVIDENCE_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")


def _now(value=None):
    current = value or datetime.now(timezone.utc)
    return current.astimezone(timezone.utc)


def _stamp(value=None):
    return _now(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _base_code(code):
    return code.split(":", 1)[0]


def _validate_evidence(evidence):
    if not isinstance(evidence, dict):
        raise ValueError("finding evidence must be an object")
    validated = {}
    for key, value in evidence.items():
        if not isinstance(key, str) or not _EVIDENCE_CODE_PATTERN.fullmatch(key):
            raise ValueError("finding evidence keys must be codes")
        if isinstance(value, bool):
            raise ValueError("finding evidence cannot contain booleans")
        if isinstance(value, int) and value >= 0:
            validated[key] = value
        elif isinstance(value, str) and _EVIDENCE_CODE_PATTERN.fullmatch(value):
            validated[key] = value
        else:
            raise ValueError("finding evidence contains non-code content")
    return validated


def make_finding(code, evidence, *, now=None):
    if not isinstance(code, str) or not _CODE_PATTERN.fullmatch(code):
        raise ValueError("finding code is invalid")
    base = _base_code(code)
    if base not in FAILURE_MODES:
        raise ValueError("finding code has no documented failure mode")
    stamp = _stamp(now)
    return {
        "id": "EP-" + hashlib.sha256(code.encode("ascii")).hexdigest()[:12],
        "code": code,
        "first_seen": stamp,
        "last_seen": stamp,
        "count": 1,
        "evidence": _validate_evidence(evidence),
        "failure_mode": FAILURE_MODES[base],
        "remedy": REMEDIES[code] if code in REMEDIES else REMEDIES[base],
    }


def findings_path(vault):
    return Path(vault) / FINDINGS_FILENAME


def _validate_record(record):
    finding = make_finding(record["code"], record["evidence"])
    if record.get("id") != finding["id"]:
        raise ValueError("finding id does not match its code")
    if record.get("status") not in STATUSES:
        raise ValueError("finding status is invalid")
    if record.get("failure_mode") != finding["failure_mode"]:
        raise ValueError("finding failure mode is invalid")
    if record.get("remedy") != finding["remedy"]:
        raise ValueError("finding remedy is invalid")
    for name in ("first_seen", "last_seen"):
        value = record.get(name)
        if not isinstance(value, str) or not value.endswith("Z"):
            raise ValueError("finding timestamp is invalid")
        datetime.fromisoformat(value[:-1] + "+00:00")
    count = record.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("finding count is invalid")
    return record


def render(records):
    rows = [
        "# Epitype Findings",
        "",
        "Machine-owned view; use graft findings ack/close instead of editing it.",
        "",
        "| id | code | status | first_seen | last_seen | count | evidence | failure_mode | remedy |",
        "|---|---|---|---|---|---:|---|---|---|",
    ]
    for record in sorted(records, key=lambda item: item["code"]):
        _validate_record(record)
        evidence = json.dumps(
            record["evidence"], ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )
        values = (
            record["id"],
            record["code"],
            record["status"],
            record["first_seen"],
            record["last_seen"],
            str(record["count"]),
            evidence,
            record["failure_mode"],
            record["remedy"],
        )
        if any("|" in value or "\n" in value for value in values):
            raise ValueError("finding table values must stay on one cell line")
        rows.append("| " + " | ".join(values) + " |")
    return "\n".join(rows) + "\n"


def load(vault):
    target = findings_path(vault)
    if not target.is_file():
        return []
    records = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| EP-"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 9:
            raise ValueError("malformed findings table row")
        evidence = json.loads(cells[6])
        record = {
            "id": cells[0],
            "code": cells[1],
            "status": cells[2],
            "first_seen": cells[3],
            "last_seen": cells[4],
            "count": int(cells[5]),
            "evidence": evidence,
            "failure_mode": cells[7],
            "remedy": cells[8],
        }
        records.append(_validate_record(record))
    if len({record["code"] for record in records}) != len(records):
        raise ValueError("duplicate finding code in machine-owned view")
    return records


def _write(vault, records):
    target = findings_path(vault)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(
        f"{target.name}.tmp-{os.getpid()}-{threading.get_ident()}"
    )
    try:
        temporary.write_text(render(records), encoding="utf-8", newline="\n")
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def merge(vault, detections, *, statuses=None, lock_timeout=0.25):
    statuses = statuses or {}
    by_code = {}
    for detection in detections:
        _validate_record({**detection, "status": "open"})
        by_code[detection["code"]] = detection
    target = findings_path(vault)
    if not by_code and target.is_file():
        return sorted(load(vault), key=lambda item: item["code"])
    with memspec.file_lock(target, lock_timeout) as acquired:
        if not acquired:
            raise OSError("findings lock unavailable")
        records = load(vault)
        current = {record["code"]: record for record in records}
        for code, detection in by_code.items():
            existing = current.get(code)
            requested_status = statuses.get(code)
            if requested_status is not None and requested_status not in STATUSES:
                raise ValueError("invalid detected finding status")
            if existing is None:
                current[code] = {
                    **detection,
                    "status": requested_status or "open",
                }
                continue
            existing["last_seen"] = detection["last_seen"]
            existing["count"] += 1
            existing["evidence"] = detection["evidence"]
            existing["failure_mode"] = detection["failure_mode"]
            existing["remedy"] = detection["remedy"]
            if requested_status is not None:
                existing["status"] = requested_status
            elif existing["status"] == "closed":
                existing["status"] = "open"
        merged = list(current.values())
        _write(vault, merged)
    return sorted(merged, key=lambda item: item["code"])


def transition(vault, action, code):
    if action not in ("ack", "close"):
        raise ValueError("unknown findings transition")
    target = findings_path(vault)
    with memspec.file_lock(target, 0.25) as acquired:
        if not acquired:
            raise OSError("findings lock unavailable")
        records = load(vault)
        matched = next((record for record in records if record["code"] == code), None)
        if matched is None:
            raise KeyError(code)
        expected, replacement = ("open", "acked") if action == "ack" else ("acked", "closed")
        if matched["status"] != expected:
            raise ValueError(f"{action} requires {expected} status")
        matched["status"] = replacement
        _write(vault, records)
    return matched


def injection_line(records):
    codes = sorted(record["code"] for record in records if record["status"] == "open")
    if not codes:
        return ""
    prefix = f"EPITYPE FINDINGS: {len(codes)} open — "
    suffix = " (see _EPITYPE_FINDINGS.md / graft doctor)"
    selected = []
    for code in codes:
        candidate = prefix + ",".join(selected + [code]) + suffix
        if len(candidate.encode("utf-8")) > 200:
            break
        selected.append(code)
    if not selected:
        selected.append(codes[0])
    line = prefix + ",".join(selected) + suffix
    if len(selected) < len(codes):
        shortened = prefix + ",".join(selected) + ",more" + suffix
        if len(shortened.encode("utf-8")) <= 200:
            line = shortened
    if len(line.encode("utf-8")) > 200:
        raise ValueError("findings injection cannot fit its hard limit")
    return line


def _parse_ts(value):
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00") if value.endswith("Z") else None
    except (TypeError, ValueError):
        return None


def _has_recent_transcript(root, cutoff, deadline, *, any_file=False):
    if not root.is_dir():
        return False
    stack = [root]
    while stack:
        if time.monotonic() >= deadline:
            return False
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            if time.monotonic() >= deadline:
                return False
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif any_file or entry.name.lower().endswith(".jsonl"):
                    modified = datetime.fromtimestamp(
                        entry.stat(follow_symlinks=False).st_mtime,
                        timezone.utc,
                    )
                    if modified >= cutoff:
                        return True
            except OSError:
                continue
    return False


def detect_host_silent(home, records, *, now=None, deadline=None, current_host=None):
    current = _now(now)
    cutoff = current - timedelta(hours=ACTIVITY_HOURS)
    deadline = deadline if deadline is not None else time.monotonic() + 60.0
    roots = {
        "claude": Path(home) / ".claude" / "projects",
        "codex": Path(home) / ".codex" / "sessions",
    }
    detections = []
    for host in ("claude", "codex"):
        if host == current_host or time.monotonic() >= deadline:
            continue
        if not _has_recent_transcript(
            roots[host], cutoff, deadline, any_file=host == "codex"
        ):
            continue
        record_signal = any(
            record.get("host") == host
            and (_parse_ts(record.get("ts")) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff
            for record in records
        )
        heartbeat_signal = False
        heartbeat_root = telemetry.state_root(home) / telemetry.HEARTBEAT_DIRECTORY
        try:
            for path in heartbeat_root.glob(f"{host}.*"):
                modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                if modified >= cutoff:
                    heartbeat_signal = True
                    break
        except OSError:
            heartbeat_signal = False
        if not record_signal and not heartbeat_signal:
            detections.append(
                make_finding(
                    f"host-silent:{host}",
                    {"activity": 1, "host": host, "signals": 0},
                    now=current,
                )
            )
    return detections


def detect_recall_miss_streak(records, *, threshold=DEFAULT_MISS_STREAK, now=None):
    streaks = {"claude": 0, "codex": 0}
    for record in records:
        if record.get("event") != "UserPromptSubmit" or record.get("host") not in streaks:
            continue
        host = record["host"]
        if record.get("terms", 0) >= 3 and record.get("hits") == 0:
            streaks[host] += 1
        else:
            streaks[host] = 0
    eligible = [(streak, host) for host, streak in streaks.items() if streak >= threshold]
    if not eligible:
        return []
    streak, host = max(eligible)
    return [
        make_finding(
            "recall-miss-streak",
            {"host": host, "streak": streak, "threshold": threshold},
            now=now,
        )
    ]


def detect_hook_internal_failures(records, *, now=None):
    current = _now(now)
    cutoff = current - timedelta(hours=ACTIVITY_HOURS)
    by_host = {}
    for record in records:
        host = record.get("host")
        reason = record.get("reason")
        timestamp = _parse_ts(record.get("ts"))
        if (
            host not in telemetry.HOSTS
            or record.get("outcome") not in ("error", "timeout")
            or reason not in HOOK_INTERNAL_REASONS
            or timestamp is None
            or timestamp < cutoff
        ):
            continue
        by_host.setdefault(host, []).append((timestamp, record))
    detections = []
    for host, failures in sorted(by_host.items()):
        latest = max(failures, key=lambda item: item[0])[1]
        detections.append(
            make_finding(
                f"hook-internal-failure:{host}",
                {
                    "failures": len(failures),
                    "host": host,
                    "reason": latest["reason"],
                    "vault_skipped": sum(row.get("vault_skipped", 0) for _, row in failures),
                },
                now=current,
            )
        )
    return detections


def _detect_fail_open(home, *, now=None):
    path = telemetry.state_root(home) / "shim_status.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = value.get("shims", {}) if isinstance(value, dict) else {}
    reasons = sorted(
        record.get("reason")
        for record in rows.values()
        if isinstance(record, dict) and record.get("reason") in SHIM_REASONS
    ) if isinstance(rows, dict) else []
    if not reasons:
        return []
    return [
        make_finding(
            "fail-open-seen",
            {"records": len(reasons), "reason": reasons[-1]},
            now=now,
        )
    ]


def _detect_codex_untrusted(home, *, now=None):
    output = io.StringIO()
    try:
        code = hook_trust.run_check(
            Path(home),
            output=output,
            seen_path=telemetry.state_root(home) / hook_trust.SEEN_FILENAME,
        )
    except Exception:
        return []
    if code == 0:
        return []
    match = re.search(r"FAIL (\d+)/(\d+)", output.getvalue())
    failing = int(match.group(1)) if match else 1
    registered = int(match.group(2)) if match else failing
    return [
        make_finding(
            "codex-untrusted",
            {"failing": failing, "host": "codex", "registered": registered},
            now=now,
        )
    ]


def _detect_index_unreachable(vaults, *, now=None, deadline=None):
    stale = []
    max_lag = 0
    for index, vault in enumerate(vaults):
        if deadline is not None and time.monotonic() >= deadline:
            break
        vault = Path(vault)
        database = vault / memspec.FTS_DB_PATH
        if not database.is_file() or not memsearch._is_stale(vault, database):
            continue
        try:
            indexed_at = database.stat().st_mtime
            latest = max(
                (path.stat().st_mtime for path in memsearch._markdown_files(vault)),
                default=indexed_at,
            )
            max_lag = max(max_lag, max(0, int(latest - indexed_at)))
        except OSError:
            pass
        stale.append((index, vault))
    if not stale:
        return [], {}
    repaired = 0
    for _, vault in stale:
        if deadline is not None and time.monotonic() >= deadline:
            break
        try:
            repaired += int(memsearch.build_index(vault, lock_timeout=0.0).get("status") == "built")
        except Exception:
            continue
    action = "auto-remedied" if repaired == len(stale) else "manual-required"
    finding = make_finding(
        "index-unreachable",
        {
            "action": action,
            "lag_seconds": max_lag,
            "repaired": repaired,
            "vaults": len(stale),
        },
        now=now,
    )
    statuses = {finding["code"]: "closed"} if action == "auto-remedied" else {}
    return [finding], statuses


def _default_collect(context):
    home = context["home"]
    now = context["now"]
    deadline = context["deadline"]
    records = telemetry.read_records(home=home)
    detections = []
    statuses = {}
    detections.extend(
        detect_host_silent(
            home,
            records,
            now=now,
            deadline=deadline,
            current_host=context["current_host"],
        )
    )
    detections.extend(detect_recall_miss_streak(records, now=now))
    detections.extend(detect_hook_internal_failures(records, now=now))
    detections.extend(_detect_fail_open(home, now=now))
    index_findings, index_statuses = _detect_index_unreachable(
        context["vaults"], now=now, deadline=deadline
    )
    detections.extend(index_findings)
    statuses.update(index_statuses)
    detections.extend(_detect_codex_untrusted(home, now=now))
    return detections, statuses


def run_detectors(
    home,
    vaults,
    *,
    current_host=None,
    now=None,
    budget_ms=DETECTOR_BUDGET_MS,
    detectors=None,
):
    started = time.monotonic()
    wait_ms = max(1, int(budget_ms) - DETECTOR_HEADROOM_MS)
    deadline = started + wait_ms / 1000.0
    result_queue = queue.Queue(maxsize=1)
    context = {
        "home": Path(home),
        "vaults": [Path(vault) for vault in vaults],
        "current_host": current_host,
        "now": _now(now),
        "deadline": deadline,
    }

    def worker():
        try:
            if detectors is None:
                result_queue.put(("ok", _default_collect(context)))
                return
            detections = []
            statuses = {}
            for detector in detectors:
                value = detector(context)
                if isinstance(value, tuple):
                    found, state = value
                    detections.extend(found)
                    statuses.update(state)
                else:
                    detections.extend(value or [])
            result_queue.put(("ok", (detections, statuses)))
        except Exception as exc:
            result_queue.put(("error", type(exc).__name__))

    thread = threading.Thread(target=worker, name="epitype-findings", daemon=True)
    thread.start()
    thread.join(max(0.0, deadline - time.monotonic()))
    elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
    if thread.is_alive():
        if current_host in telemetry.HOSTS:
            telemetry.append(
                current_host,
                "FindingsDetector",
                "error",
                ms=elapsed_ms,
                reason="detector-timeout",
                home=home,
            )
        finding = make_finding(
            "detector-timeout",
            {"budget_ms": int(budget_ms)},
            now=now,
        )
        return [finding], {}, True
    try:
        status, payload = result_queue.get_nowait()
    except queue.Empty:
        status, payload = "error", "empty-result"
    if status != "ok":
        if current_host in telemetry.HOSTS:
            telemetry.append(
                current_host,
                "FindingsDetector",
                "error",
                ms=elapsed_ms,
                reason="detector-error",
                home=home,
            )
        return [], {}, False
    detections, statuses = payload
    deduplicated = {finding["code"]: finding for finding in detections}
    return list(deduplicated.values()), statuses, False


def run_and_record(
    home,
    vaults,
    *,
    current_host=None,
    now=None,
    budget_ms=DETECTOR_BUDGET_MS,
    detectors=None,
):
    vaults = [Path(vault) for vault in vaults]
    if not vaults:
        return [], False
    detections, statuses, timed_out = run_detectors(
        home,
        vaults,
        current_host=current_host,
        now=now,
        budget_ms=budget_ms,
        detectors=detectors,
    )
    records = merge(vaults[0], detections, statuses=statuses, lock_timeout=0.005)
    return records, timed_out


def _telemetry_row(host="claude", hits=0, terms=3, ts="2026-09-02T00:00:00Z"):
    return {
        "ts": ts,
        "host": host,
        "event": "UserPromptSubmit",
        "outcome": "hit" if hits else "miss",
        "hits": hits,
        "injected_bytes": 0,
        "terms": terms,
        "vaults": 1,
        "vault_skipped": 0,
        "ms": 1,
        "reason": "context-injected" if hits else "no-hit",
    }


def _selftest():
    checks = []
    hot_numbers = None
    timeout_number = None
    try:
        with tempfile.TemporaryDirectory(prefix="epitype-findings-") as temp_dir:
            root = Path(temp_dir)
            now = datetime(2026, 9, 2, 4, 0, tzinfo=timezone.utc)

            silent_home = root / "silent-home"
            transcript = silent_home / ".codex" / "sessions" / "2026" / "09" / "02" / "sample.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text("{}\n", encoding="utf-8")
            os.utime(transcript, (now.timestamp(), now.timestamp()))
            silent = detect_host_silent(silent_home, [], now=now)
            checks.append(
                (
                    "recent session with no hook signal becomes host-silent for the right host",
                    len(silent) == 1
                    and silent[0]["code"] == "host-silent:codex"
                    and silent[0]["evidence"]["host"] == "codex",
                )
            )

            eight = [_telemetry_row() for _ in range(8)]
            seven = [_telemetry_row() for _ in range(7)]
            reset_seven = eight + [_telemetry_row(hits=1)] + seven
            reset_eight = eight + [_telemetry_row(hits=1)] + eight
            checks.append(
                (
                    "miss streak threshold and hit reset",
                    bool(detect_recall_miss_streak(eight, now=now))
                    and not detect_recall_miss_streak(seven, now=now)
                    and not detect_recall_miss_streak(reset_seven, now=now)
                    and detect_recall_miss_streak(reset_eight, now=now)[0]["evidence"]["streak"] == 8,
                )
            )

            internal_row = _telemetry_row(ts="2026-09-02T03:59:00Z")
            internal_row.update(
                outcome="error", reason="no-index", vault_skipped=1
            )
            internal = detect_hook_internal_failures([internal_row], now=now)
            legacy_row = _telemetry_row()
            legacy_row.pop("vault_skipped")
            normalized_legacy_row = telemetry._validated_record(legacy_row)
            checks.append(
                (
                    "hook-internal telemetry becomes a bounded finding",
                    len(internal) == 1
                    and internal[0]["code"] == "hook-internal-failure:claude"
                    and internal[0]["evidence"]["reason"] == "no-index"
                    and internal[0]["evidence"]["vault_skipped"] == 1
                    and normalized_legacy_row["vault_skipped"] == 0,
                )
            )

            state_vault = root / "state-vault"
            state_vault.mkdir()
            first = make_finding("recall-miss-streak", {"host": "claude", "streak": 8}, now=now)
            first_rows = merge(state_vault, [first])
            later = now + timedelta(minutes=5)
            second = make_finding("recall-miss-streak", {"host": "claude", "streak": 9}, now=later)
            second_rows = merge(state_vault, [second])
            checks.append(
                (
                    "repeat detection updates one row",
                    len(first_rows) == len(second_rows) == 1
                    and second_rows[0]["count"] == 2
                    and second_rows[0]["first_seen"] == first_rows[0]["first_seen"]
                    and second_rows[0]["last_seen"] != first_rows[0]["last_seen"],
                )
            )

            transition(state_vault, "ack", "recall-miss-streak")
            transition(state_vault, "close", "recall-miss-streak")
            reopened = merge(
                state_vault,
                [make_finding("recall-miss-streak", {"host": "claude", "streak": 10}, now=later + timedelta(minutes=5))],
            )[0]
            checks.append(
                (
                    "closed finding reopens and increments",
                    reopened["status"] == "open" and reopened["count"] == 3,
                )
            )

            line = injection_line([reopened])
            closed = [{**reopened, "status": "closed"}]
            checks.append(
                (
                    "bounded findings injection and zero-open silence",
                    "recall-miss-streak" in line
                    and len(line.encode("utf-8")) <= 200
                    and injection_line(closed) == "",
                )
            )

            inherited_home = root / "inherited-home"
            hook_trust._fixture(inherited_home, {})
            shim_status = telemetry.state_root(inherited_home) / "shim_status.json"
            shim_status.parent.mkdir(parents=True, exist_ok=True)
            shim_status.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "shims": {
                            "recall.py": {
                                "timestamp": "2026-09-02T00:00:00Z",
                                "shim": "recall.py",
                                "reason": "adapter_missing",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            index_vault = root / "index-vault"
            index_vault.mkdir()
            (index_vault / "initial.md").write_text(
                "---\nname: Initial\ndescription: initial fixture\n---\ninitial fixture\n",
                encoding="utf-8",
            )
            memsearch.build_index(index_vault)
            late_card = index_vault / "late.md"
            late_card.write_text(
                "---\nname: Late\ndescription: lateindexneedle\n---\nlateindexneedle\n",
                encoding="utf-8",
            )
            old_index_time = time.time() - memspec.FTS_STALE_SECONDS - 5
            os.utime(index_vault / memspec.FTS_DB_PATH, (old_index_time, old_index_time))
            inherited_fail_open = _detect_fail_open(inherited_home, now=now)
            inherited_untrusted = _detect_codex_untrusted(inherited_home, now=now)
            inherited_index, inherited_statuses = _detect_index_unreachable(
                [index_vault], now=now, deadline=time.monotonic() + 1.0
            )
            checks.append(
                (
                    "U12-U15 detector integrations become bounded findings",
                    inherited_fail_open[0]["code"] == "fail-open-seen"
                    and inherited_untrusted[0]["code"] == "codex-untrusted"
                    and inherited_index[0]["code"] == "index-unreachable"
                    and inherited_index[0]["evidence"]["action"] == "auto-remedied"
                    and inherited_statuses == {"index-unreachable": "closed"}
                    and memsearch.query_index(index_vault, "lateindexneedle")["count"] == 1,
                )
            )

            canary_home = root / "canary-home"
            canary_vault = root / "canary-vault"
            canary_vault.mkdir()
            (canary_vault / "card.md").write_text(
                "---\nname: Synthetic Recall\ndescription: benign recall fixture\n---\nbenign recall fixture\n",
                encoding="utf-8",
            )
            memsearch.build_index(canary_vault)
            canary_config = root / "canary-config.json"
            canary_config.write_text(json.dumps({"vaults": [os.fspath(canary_vault)]}), encoding="utf-8")
            canary = "EPITYPE-PRIVATE-CANARY-DO-NOT-STORE"
            environment = os.environ.copy()
            environment["EPITYPE_CONFIG"] = os.fspath(canary_config)
            environment[telemetry.TEST_HOME_ENV] = os.fspath(canary_home)
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            recall_result = subprocess.run(
                [
                    sys.executable,
                    os.fspath(_REPO_ROOT / "adapters" / "claude" / "recall_hook.py"),
                    "--codex",
                ],
                input=json.dumps({"prompt": canary + " benign recall fixture"}),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                cwd=_REPO_ROOT,
                timeout=10,
                check=False,
            )
            compact_transcript = root / "canary-transcript.jsonl"
            compact_transcript.write_text(
                json.dumps({"type": "user", "message": {"content": "synthetic compact fixture"}}) + "\n",
                encoding="utf-8",
            )
            compact_result = subprocess.run(
                [
                    sys.executable,
                    os.fspath(_REPO_ROOT / "adapters" / "claude" / "precompact_hook.py"),
                    "--codex",
                ],
                input=json.dumps({"transcript_path": os.fspath(compact_transcript)}),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                cwd=_REPO_ROOT,
                timeout=10,
                check=False,
            )
            (canary_vault / "trigger.md").write_text(
                "---\n"
                "name: synthetic-deny\n"
                "trigger:\n"
                "  tool: ^SyntheticWrite$\n"
                f"  input: {canary}\n"
                "advice: Use the synthetic read-only route.\n"
                "---\n",
                encoding="utf-8",
            )
            deny_result = subprocess.run(
                [
                    sys.executable,
                    os.fspath(_REPO_ROOT / "adapters" / "claude" / "pretooluse_gate.py"),
                    "--codex",
                ],
                input=json.dumps(
                    {"tool_name": "SyntheticWrite", "tool_input": {"value": canary}}
                ),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                cwd=_REPO_ROOT,
                timeout=10,
                check=False,
            )
            merge(canary_vault, [])
            telemetry_text = telemetry.telemetry_path(canary_home).read_text(encoding="ascii")
            findings_text = findings_path(canary_vault).read_text(encoding="utf-8")
            canary_records = telemetry.read_records(home=canary_home)
            checks.append(
                (
                    "privacy canary never reaches telemetry or findings",
                    recall_result.returncode == 0
                    and compact_result.returncode == 0
                    and deny_result.returncode == 0
                    and canary not in telemetry_text
                    and canary not in findings_text
                    and {record["event"] for record in canary_records}
                    == {"UserPromptSubmit", "PreCompact", "PreToolUse"}
                    and all(set(record) == set(telemetry.FIELDS) for record in canary_records)
                    and all(record["host"] == "codex" for record in canary_records)
                    and any(
                        record["event"] == "PreToolUse" and record["outcome"] == "deny"
                        for record in canary_records
                    ),
                )
            )

            adapter_directory = _REPO_ROOT / "adapters" / "claude"
            if os.fspath(adapter_directory) not in sys.path:
                sys.path.insert(0, os.fspath(adapter_directory))
            from adapters.claude import pretooluse_gate

            hot_home = root / "hot-home"
            hot_vault = root / "hot-vault"
            hot_vault.mkdir()
            hot_config = root / "hot-config.json"
            hot_config.write_text(json.dumps({"vaults": [os.fspath(hot_vault)]}), encoding="utf-8")
            old_config = os.environ.get("EPITYPE_CONFIG")
            old_test_home = os.environ.get(telemetry.TEST_HOME_ENV)
            os.environ["EPITYPE_CONFIG"] = os.fspath(hot_config)
            os.environ[telemetry.TEST_HOME_ENV] = os.fspath(hot_home)
            event = {"tool_name": "SyntheticRead", "tool_input": {"path": "synthetic.txt"}}
            try:
                baseline_started = time.perf_counter()
                for _ in range(200):
                    pretooluse_gate._handle(event, time.monotonic())
                baseline_ms = (time.perf_counter() - baseline_started) * 1000.0 / 200.0
                before_lines = len(telemetry.read_records(home=hot_home))
                touches = 0
                actual_started = time.perf_counter()
                for _ in range(200):
                    _, touched = pretooluse_gate._process(event, time.monotonic(), "claude", home=hot_home)
                    touches += int(touched)
                actual_ms = (time.perf_counter() - actual_started) * 1000.0 / 200.0
                after_lines = len(telemetry.read_records(home=hot_home))
            finally:
                if old_config is None:
                    os.environ.pop("EPITYPE_CONFIG", None)
                else:
                    os.environ["EPITYPE_CONFIG"] = old_config
                if old_test_home is None:
                    os.environ.pop(telemetry.TEST_HOME_ENV, None)
                else:
                    os.environ[telemetry.TEST_HOME_ENV] = old_test_home
            delta_ms = actual_ms - baseline_ms
            hot_numbers = (baseline_ms, actual_ms, delta_ms, touches)
            checks.append(
                (
                    "PreToolUse allow heartbeat is bounded",
                    before_lines == after_lines and touches <= 1 and delta_ms <= 5.0,
                )
            )

            timeout_home = root / "timeout-home"
            timeout_vault = root / "timeout-vault"
            timeout_vault.mkdir()

            def slow_detector(_context):
                time.sleep(0.3)
                return []

            timeout_started = time.perf_counter()
            timeout_rows, timed_out = run_and_record(
                timeout_home,
                [timeout_vault],
                current_host="claude",
                budget_ms=150,
                detectors=(slow_detector,),
            )
            timeout_elapsed_ms = (time.perf_counter() - timeout_started) * 1000.0
            timeout_number = timeout_elapsed_ms
            timeout_records = telemetry.read_records(home=timeout_home)
            checks.append(
                (
                    "detector timeout stays inside budget and is recorded",
                    timed_out
                    and timeout_elapsed_ms <= 150.0
                    and timeout_rows[0]["code"] == "detector-timeout"
                    and any(record["reason"] == "detector-timeout" for record in timeout_records),
                )
            )

            rotate_home = root / "rotate-home"
            rotate_vault = root / "rotate-vault"
            rotate_vault.mkdir()
            row = json.dumps(_telemetry_row(), separators=(",", ":")) + "\n"
            rotate_path = telemetry.telemetry_path(rotate_home)
            rotate_path.parent.mkdir(parents=True)
            rotate_path.write_text(row * ((telemetry.MAX_BYTES // len(row)) + 50), encoding="ascii")
            rotate_config = root / "rotate-config.json"
            rotate_config.write_text(json.dumps({"vaults": [os.fspath(rotate_vault)]}), encoding="utf-8")
            rotate_environment = os.environ.copy()
            rotate_environment["EPITYPE_CONFIG"] = os.fspath(rotate_config)
            rotate_environment[telemetry.TEST_HOME_ENV] = os.fspath(rotate_home)
            rotate_environment["PYTHONDONTWRITEBYTECODE"] = "1"
            rotate_result = subprocess.run(
                [sys.executable, os.fspath(_REPO_ROOT / "adapters" / "claude" / "sessionstart_hook.py")],
                input="{}",
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=rotate_environment,
                cwd=_REPO_ROOT,
                timeout=10,
                check=False,
            )
            rotated_bytes = rotate_path.read_bytes()
            complete_rows = [json.loads(line) for line in rotated_bytes.splitlines()]
            checks.append(
                (
                    "SessionStart rotates to complete bounded tail rows",
                    rotate_result.returncode == 0
                    and len(rotated_bytes) <= telemetry.KEEP_BYTES
                    and bool(complete_rows)
                    and rotated_bytes.endswith(b"\n"),
                )
            )
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    if hot_numbers is not None:
        baseline_ms, actual_ms, delta_ms, touches = hot_numbers
        print(
            "HOTPATH PreToolUse allow x200 "
            f"baseline_avg_ms={baseline_ms:.3f} actual_avg_ms={actual_ms:.3f} "
            f"delta_avg_ms={delta_ms:.3f} telemetry_lines=0 heartbeat_touches={touches}"
        )
    if timeout_number is not None:
        print(f"DETECTOR timeout_elapsed_ms={timeout_number:.3f} budget_ms=150")
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
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    return _selftest() if args.selftest else 0


if __name__ == "__main__":
    raise SystemExit(main())
