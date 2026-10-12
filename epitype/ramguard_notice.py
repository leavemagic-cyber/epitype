import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8。
"""RAMGuard 撞上限告警：新對話開場提醒，以及 owner 回覆後的確認。

RAMGuard 撞到 AI 對話的程序數或記憶體上限時，會在 alerts.jsonl 寫一列告警。owner 要求：
撞過上限、還沒處理的，任何一個新的 Claude 或 Codex 對話一開始就要知道，並主動問要不要
調高上限。開場（SessionStart，兩邊共用）讀設定鍵 ``ramguard_alerts_path`` 指的檔案尾端，
列出最近 7 天、還沒確認的告警；``python -m epitype ramguard-ack`` 把目前最新的告警時間
記成已確認。這是提醒，不擋任何事；任何錯誤都當作沒有告警，不能影響開場。
解除條件見 memspec.CONFIG_RAMGUARD_ALERTS_FIELD 旁的註解。
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

try:
    from . import memspec
except ImportError:  # Direct script execution keeps the CLI contract.
    import memspec

REPO_ROOT = Path(__file__).resolve().parents[1]
DAY_SECONDS = 24 * 60 * 60


def alerts_path(value):
    """The configured alerts file, or None when the key is unset, blank or not a string."""
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value.strip()).expanduser()


def ack_path(state_dir=None):
    """The acknowledgement state file, next to the Epitype configuration (~/.epitype)."""
    base = Path(state_dir) if state_dir is not None else memspec.config_path().parent
    return base / memspec.RAMGUARD_ACK_FILENAME


def read_tail(path, max_bytes=memspec.RAMGUARD_TAIL_BYTES):
    """Complete lines from the last `max_bytes` of `path`; never reads more than that.

    A cut that lands mid-line drops that partial first line rather than parsing half a row."""
    with Path(path).open("rb") as stream:
        size = stream.seek(0, os.SEEK_END)
        start = max(0, size - max_bytes)
        stream.seek(start)
        data = stream.read(max_bytes)
    if start > 0:
        cut = data.find(b"\n")
        data = data[cut + 1:] if cut >= 0 else b""
    return data.decode("utf-8", errors="replace").splitlines()


def _epoch(item):
    value = item.get("ts_epoch")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = item.get("ts")
    if isinstance(text, str):
        try:
            return datetime.fromisoformat(text.strip().replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError, OSError):
            return None
    return None


def parse_alerts(lines):
    """Limit-hit alerts in file order, as dicts with kind, ai, epoch and limit.

    Every other row (RAMGuard also logs memory growth and other events there) is skipped,
    as is any row that is not JSON or carries no usable time."""
    alerts = []
    for line in lines:
        if "ai_session_" not in line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind not in memspec.RAMGUARD_LIMIT_KINDS:
            continue
        epoch = _epoch(item)
        if epoch is None:
            continue
        field, _unit = memspec.RAMGUARD_LIMIT_FIELDS[kind]
        ai = item.get("session_kind")
        alerts.append({"kind": kind, "ai": ai if isinstance(ai, str) and ai else "?",
                       "epoch": epoch, "limit": item.get(field)})
    return alerts


def acked_through(path):
    """The acknowledged-through time, or None when nothing was acknowledged yet."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8")).get("acked_through_epoch")
    except (OSError, ValueError, AttributeError):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def unacknowledged(alerts, now, acked=None, days=memspec.RAMGUARD_WINDOW_DAYS):
    """Alerts inside the last `days` days and later than the acknowledged time."""
    start = now - days * DAY_SECONDS
    return [alert for alert in alerts
            if alert["epoch"] >= start and (acked is None or alert["epoch"] > acked)]


def _stamp(epoch):
    return datetime.fromtimestamp(epoch).strftime("%m/%d %H:%M")


def _limit_text(kind, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "未記錄"
    _field, unit = memspec.RAMGUARD_LIMIT_FIELDS[kind]
    return f"{value:g}{unit}"


def notice_text(alerts, repo=REPO_ROOT):
    """One line of at most RAMGUARD_NOTICE_MAX_CHARS characters, or None for no alerts.

    Grouped by (AI, limit kind), newest group first; groups that do not fit are counted."""
    if not alerts:
        return None
    groups = {}
    for alert in alerts:
        group = groups.setdefault((alert["ai"], alert["kind"]), {"count": 0, "latest": alert})
        group["count"] += 1
        if alert["epoch"] >= group["latest"]["epoch"]:
            group["latest"] = alert
    ordered = sorted(groups.items(), key=lambda item: item[1]["latest"]["epoch"], reverse=True)
    parts = [memspec.RAMGUARD_NOTICE_GROUP.format(
        ai=memspec.RAMGUARD_AI_NAMES.get(ai, ai[:12]), what=memspec.RAMGUARD_LIMIT_KINDS[kind],
        count=group["count"], latest=_stamp(group["latest"]["epoch"]),
        limit=_limit_text(kind, group["latest"]["limit"]))
        for (ai, kind), group in ordered]
    text = None
    for shown in range(len(parts), -1, -1):
        listed = parts[:shown]
        if shown < len(parts):
            listed.append(memspec.RAMGUARD_NOTICE_MORE.format(count=len(parts) - shown))
        text = memspec.RAMGUARD_NOTICE.format(
            days=memspec.RAMGUARD_WINDOW_DAYS, groups="；".join(listed), repo=repo)
        if len(text) <= memspec.RAMGUARD_NOTICE_MAX_CHARS:
            return text
    return text[: memspec.RAMGUARD_NOTICE_MAX_CHARS - 1] + "…"


def startup_notice(config, now=None, state_dir=None):
    """The SessionStart line, or None. Never raises: a failure here must not touch the opening."""
    try:
        path = alerts_path((config or {}).get(memspec.CONFIG_RAMGUARD_ALERTS_FIELD))
        if path is None or not path.is_file():
            return None
        now = time.time() if now is None else now
        pending = unacknowledged(parse_alerts(read_tail(path)), now,
                                 acked_through(ack_path(state_dir)))
        return notice_text(pending)
    except Exception:
        return None


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).astimezone().isoformat(timespec="seconds")


def acknowledge(now=None, state_dir=None, output=None):
    """Record the newest limit alert's time as acknowledged. Exit 0 also when there is none."""
    output = sys.stdout if output is None else output
    try:
        config = json.loads(memspec.config_path().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        print(memspec.RAMGUARD_ACK_NONE.format(reason=f"讀不到設定檔（{type(exc).__name__}）"), file=output)
        return 2
    path = alerts_path(config.get(memspec.CONFIG_RAMGUARD_ALERTS_FIELD) if isinstance(config, dict) else None)
    if path is None:
        print(memspec.RAMGUARD_ACK_NONE.format(
            reason=f"設定檔沒有 {memspec.CONFIG_RAMGUARD_ALERTS_FIELD}"), file=output)
        return 2
    if not path.is_file():
        print(memspec.RAMGUARD_ACK_NONE.format(reason=f"{path} 不存在"), file=output)
        return 2
    alerts = parse_alerts(read_tail(path))
    if not alerts:
        print(memspec.RAMGUARD_ACK_NONE.format(reason=f"{path} 檔尾沒有撞上限的告警"), file=output)
        return 0
    target = ack_path(state_dir)
    latest = max(alert["epoch"] for alert in alerts)
    previous = acked_through(target)
    latest = max(latest, previous) if previous is not None else latest
    now = time.time() if now is None else now
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name("." + target.name + ".tmp-%d" % os.getpid())
    staging.write_text(json.dumps({
        "acked_through_epoch": latest, "acked_through": _iso(latest),
        "acked_at": _iso(now), "alerts_path": str(path),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(staging, target)
    print(memspec.RAMGUARD_ACK_DONE.format(stamp=_iso(latest), count=len(alerts)), file=output)
    return 0


def main(argv=None, output=None):
    import argparse  # Only the CLI needs it; the SessionStart path must stay cheap.

    parser = argparse.ArgumentParser(
        prog="epitype ramguard-ack",
        description="把 RAMGuard 目前最新的撞上限告警時間記成已確認（開場提醒就不再列它與更早的告警）。")
    parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    return acknowledge(output=output)


if __name__ == "__main__":
    raise SystemExit(main())
