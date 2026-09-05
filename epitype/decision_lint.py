import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8；唯讀 CLI 不落 pyc。
"""Epitype 決策卡唯一性、取代鏈、決策者與日期 lint。"""

import argparse
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
import posixpath
import re
import tempfile

try:
    from . import memspec as _memspec
    from . import memsearch as _memsearch
except ImportError:  # Direct script execution remains supported.
    import memspec as _memspec
    import memsearch as _memsearch

ACTIVE_DECISION_STATUS = _memspec.ACTIVE_DECISION_STATUS
AI_AUTONOMOUS_DECIDER = _memspec.AI_AUTONOMOUS_DECIDER
CURRENT_DECISION_AT_FIELD = _memspec.CURRENT_DECISION_AT_FIELD
DECIDED_BY_FIELD = _memspec.DECIDED_BY_FIELD
DECIDED_BY_VALUES = _memspec.DECIDED_BY_VALUES
DECISION_KEY_FIELD = _memspec.DECISION_KEY_FIELD
DECISION_STATUS_FIELD = _memspec.DECISION_STATUS_FIELD
DECISION_STATUS_VALUES = _memspec.DECISION_STATUS_VALUES
OWNER_EXPLICIT_DECIDER = _memspec.OWNER_EXPLICIT_DECIDER
OWNER_QUOTE_FIELD = _memspec.OWNER_QUOTE_FIELD
SUPERSEDED_BY_FIELD = _memspec.SUPERSEDED_BY_FIELD
SUPERSEDED_DECISION_STATUS = _memspec.SUPERSEDED_DECISION_STATUS

FRONTMATTER_BOUNDARY = "---"
YAML_DOCUMENT_END = "..."
TOP_LEVEL_FIELD = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$")


@dataclass(frozen=True)
class Card:
    path: Path
    fields: dict[str, str]


@dataclass(frozen=True)
class Finding:
    level: str
    path_text: str
    rule: str
    reason: str


@dataclass
class LintReport:
    vault: Path
    cards: list[Card] = field(default_factory=list)
    failures: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)
    audit_rows: list[Card] = field(default_factory=list)

    @property
    def exit_code(self):
        return 1 if self.failures else 0


def _strip_inline_comment(value):
    """移除 plain scalar 的 YAML 行尾註解，保留引號內的井字號。"""
    quote = None
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if quote == '"' and character == "\\":
            escaped = True
            continue
        if character in ("'", '"'):
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            continue
        if character == "#" and quote is None and (
            index == 0 or value[index - 1].isspace()
        ):
            return value[:index].rstrip()
    return value.rstrip()


def _parse_scalar(raw_value):
    value = _strip_inline_comment(raw_value).strip()
    if not value:
        return "", None
    if value[0] == "'":
        if len(value) < 2 or value[-1] != "'":
            return "", "單引號字串未閉合"
        return value[1:-1].replace("''", "'"), None
    if value[0] == '"':
        if len(value) < 2 or value[-1] != '"':
            return "", "雙引號字串未閉合"
        try:
            # 決策欄位只需 scalar；unicode_escape 不適合中文，因此只處理常見 YAML 跳脫。
            inner = value[1:-1]
            inner = inner.replace("\\\"", '"').replace("\\\\", "\\")
            return inner, None
        except ValueError:
            return "", "雙引號字串無法解析"
    return value, None


def _parse_frontmatter(path):
    """回傳 top-level scalar 欄位；壞 YAML 留一則警告但保留已解析欄。"""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        return {}, f"無法以 UTF-8 讀取 frontmatter：{type(exc).__name__}"

    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER_BOUNDARY:
        return {}, None

    closing_index = None
    for index in range(1, len(lines)):
        if lines[index].strip() in (FRONTMATTER_BOUNDARY, YAML_DOCUMENT_END):
            closing_index = index
            break
    if closing_index is None:
        return {}, "frontmatter 缺少結束界線"

    fields = {}
    problems = []
    active_container_indent = None
    block_field = None
    block_indent = None
    block_lines = []

    def finish_block():
        nonlocal block_field, block_indent, block_lines
        if block_field is not None:
            fields[block_field] = "\n".join(block_lines).strip()
        block_field = None
        block_indent = None
        block_lines = []

    for line_number, raw_line in enumerate(lines[1:closing_index], start=2):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            if block_field is not None:
                block_lines.append("")
            continue

        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if "\t" in raw_line[: len(raw_line) - len(raw_line.lstrip())]:
            problems.append(f"L{line_number} 使用 tab 縮排")
            continue

        if block_field is not None:
            if indent > 0:
                if block_indent is None:
                    block_indent = indent
                block_lines.append(raw_line[min(indent, block_indent) :])
                continue
            finish_block()

        if indent > 0:
            if active_container_indent is not None:
                continue
            problems.append(f"L{line_number} 有無上層欄位的縮排內容")
            continue

        active_container_indent = None
        match = TOP_LEVEL_FIELD.match(raw_line)
        if match is None:
            problems.append(f"L{line_number} 不是 top-level key: value")
            continue

        key, raw_value = match.groups()
        if key in fields:
            problems.append(f"L{line_number} 重複欄位 {key}")
            continue
        stripped = raw_value.strip()
        if stripped in ("|", ">", "|-", ">-", "|+", ">+"):
            block_field = key
            block_indent = None
            block_lines = []
            continue

        value, problem = _parse_scalar(raw_value)
        fields[key] = value
        if problem:
            problems.append(f"L{line_number} {problem}")
        if not stripped:
            active_container_indent = 0

    finish_block()
    if problems:
        return fields, "；".join(problems[:3])
    return fields, None


def _is_iso_date(value):
    if not value:
        return False
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        pass
    try:
        normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
        datetime.fromisoformat(normalized)
        return True
    except ValueError:
        return False


def _target_index(cards, vault):
    exact = {}
    loose = {}
    for card in cards:
        relative = card.path.relative_to(vault).as_posix()
        exact_identities = {
            relative,
            Path(relative).with_suffix("").as_posix(),
        }
        loose_identities = {
            card.path.name,
            card.path.stem,
        }
        for identity in exact_identities:
            exact.setdefault(identity.casefold(), set()).add(card.path)
        for identity in loose_identities:
            loose.setdefault(identity.casefold(), set()).add(card.path)
    return exact, loose


def _chain_loops(card, replacement, vault, target_index, cards_by_path):
    """A superseded card may point at a card that was itself superseded later;
    the chain is valid as long as it never returns to a card already visited.
    Any other break in the chain is reported on the card that carries it."""
    seen = {card.path}
    current = replacement
    while current.fields.get(DECISION_STATUS_FIELD, "").strip() != ACTIVE_DECISION_STATUS:
        if current.path in seen:
            return True
        seen.add(current.path)
        target = current.fields.get(SUPERSEDED_BY_FIELD, "").strip()
        matches = _resolve_target(current.path, target, vault, target_index) if target else set()
        if len(matches) != 1:
            return False
        current = cards_by_path[next(iter(matches))]
    return False


def _resolve_target(source, raw_target, vault, target_index):
    target = posixpath.normpath(raw_target.strip().replace("\\", "/"))
    if not target or target == "." or target == ".." or target.startswith("../"):
        return set()
    variants = {target}
    if Path(target).suffix.casefold() != ".md":
        variants.add(target + ".md")
    try:
        relative_parent = source.parent.relative_to(vault).as_posix()
    except ValueError:
        relative_parent = ""

    exact, loose = target_index

    # 先尊重來源卡的相對路徑；精確命中時不得被別處同 basename 的卡污染。
    if relative_parent and relative_parent != ".":
        source_relative = {
            posixpath.normpath(f"{relative_parent}/{item}") for item in variants
        }
        matches = set()
        for variant in source_relative:
            matches.update(exact.get(variant.casefold(), set()))
        if matches:
            return matches

    matches = set()
    for variant in variants:
        matches.update(exact.get(variant.casefold(), set()))
    if matches:
        return matches

    # 沒寫路徑且前兩層都落空時，才以 basename／stem 相容舊卡片指標。
    if "/" not in target:
        for variant in variants:
            matches.update(loose.get(Path(variant).name.casefold(), set()))
    return matches


def lint_vault(vault, audit=False):
    vault = Path(vault).resolve()
    report = LintReport(vault=vault)
    if not vault.is_dir():
        report.failures.append(
            Finding("FAIL", str(vault), "輸入", "vault 目錄不存在或不是目錄")
        )
        return report

    for path in _memsearch.card_files(vault):
        fields, yaml_problem = _parse_frontmatter(path)
        if yaml_problem:
            report.warnings.append(
                Finding("WARN", str(path), "YAML", f"壞 YAML：{yaml_problem}")
            )
        # 無 decision_key 的普通筆記不是決策卡，靜默略過。
        if DECISION_KEY_FIELD not in fields:
            continue
        # 保留 vault 內的字面路徑；resolve 可能讓 symlink 逃出 vault，破壞相對索引。
        report.cards.append(Card(path=path.absolute(), fields=fields))

    groups = {}
    for card in report.cards:
        decision_key = card.fields.get(DECISION_KEY_FIELD, "").strip()
        groups.setdefault(decision_key, []).append(card)

        if not decision_key:
            report.failures.append(
                Finding("FAIL", str(card.path), "1", "decision_key 不得空白")
            )
            continue

        status = card.fields.get(DECISION_STATUS_FIELD, "").strip()
        if status not in DECISION_STATUS_VALUES:
            shown = status or "<缺欄>"
            report.failures.append(
                Finding("FAIL", str(card.path), "1", f"status 不在 memspec 值域：{shown}")
            )

        decided_by = card.fields.get(DECIDED_BY_FIELD, "").strip()
        if decided_by not in DECIDED_BY_VALUES:
            shown = decided_by or "<缺欄>"
            report.failures.append(
                Finding("FAIL", str(card.path), "3", f"decided_by 不在 memspec 值域：{shown}")
            )
        elif decided_by == OWNER_EXPLICIT_DECIDER and not card.fields.get(
            OWNER_QUOTE_FIELD, ""
        ).strip():
            # 2026-09-01 實測事故：表決門檻被擅自增加，導致現行裁定遭改寫；
            # 規則：明示裁定必須附可稽核的逐字引文。
            report.failures.append(
                Finding("FAIL", str(card.path), "3", "owner-explicit 缺 owner_quote 逐字原話")
            )

        timestamp = card.fields.get(CURRENT_DECISION_AT_FIELD, "").strip()
        if not _is_iso_date(timestamp):
            shown = timestamp or "<缺欄>"
            report.failures.append(
                Finding("FAIL", str(card.path), "4", f"current_decision_at 不是可解析 ISO 日期：{shown}")
            )

    # 2026-09-01 實測事故：同一 decision_key 留有多張現行卡，導致後續工具可能
    # 採用過期規則；規則：同一 decision_key 只能有一張 active 現行卡。
    for decision_key, cards in sorted(groups.items(), key=lambda item: item[0].casefold()):
        active_cards = [
            card
            for card in cards
            if card.fields.get(DECISION_STATUS_FIELD, "").strip()
            == ACTIVE_DECISION_STATUS
        ]
        shown_key = decision_key or "<空白 decision_key>"
        if not active_cards:
            # A key whose every card is retired is history, not a defect: the
            # exam corpus and real vaults keep such keys (a stale pointer on one
            # of them is already a rule-2 failure).
            paths = "；".join(str(card.path) for card in cards)
            report.warnings.append(
                Finding("WARN", paths, "1", f"decision_key={shown_key} 沒有現行卡")
            )
        elif len(active_cards) > 1:
            paths = "；".join(str(card.path) for card in active_cards)
            report.failures.append(
                Finding(
                    "FAIL",
                    paths,
                    "1",
                    f"decision_key={shown_key} 有 {len(active_cards)} 張現行卡",
                )
            )

    target_index = _target_index(report.cards, vault)
    cards_by_path = {card.path: card for card in report.cards}
    for card in report.cards:
        if (
            card.fields.get(DECISION_STATUS_FIELD, "").strip()
            != SUPERSEDED_DECISION_STATUS
        ):
            continue
        target = card.fields.get(SUPERSEDED_BY_FIELD, "").strip()
        if not target:
            report.failures.append(
                Finding("FAIL", str(card.path), "2", "superseded 卡缺 superseded_by")
            )
            continue
        matches = _resolve_target(card.path, target, vault, target_index)
        if matches == {card.path}:
            report.failures.append(
                Finding("FAIL", str(card.path), "2", "superseded_by 不得指向自己")
            )
        elif not matches:
            report.failures.append(
                Finding("FAIL", str(card.path), "2", f"superseded_by 指向不存在的卡：{target}")
            )
        elif len(matches) > 1:
            targets = "；".join(str(path) for path in sorted(matches))
            report.failures.append(
                Finding("FAIL", str(card.path), "2", f"superseded_by 指向不唯一：{targets}")
            )
        else:
            replacement = cards_by_path[next(iter(matches))]
            if (
                replacement.fields.get(DECISION_KEY_FIELD, "").strip()
                != card.fields.get(DECISION_KEY_FIELD, "").strip()
            ):
                report.failures.append(
                    Finding("FAIL", str(card.path), "2", "superseded_by 必須指向相同 decision_key")
                )
            elif _chain_loops(card, replacement, vault, target_index, cards_by_path):
                report.failures.append(
                    Finding("FAIL", str(card.path), "2", "superseded_by 鏈形成循環，沒有現行卡可到達")
                )

    if audit:
        report.audit_rows = sorted(
            (
                card
                for card in report.cards
                if card.fields.get(DECISION_STATUS_FIELD, "").strip()
                == ACTIVE_DECISION_STATUS
                and card.fields.get(DECIDED_BY_FIELD, "").strip()
                != OWNER_EXPLICIT_DECIDER
            ),
            key=lambda card: str(card.path).casefold(),
        )
    return report


def render_report(report, audit=False):
    lines = []
    for finding in report.failures:
        lines.append(
            f"FAIL: {finding.path_text} | 規則{finding.rule} | {finding.reason}"
        )
    for finding in report.warnings:
        lines.append(
            f"WARN: {finding.path_text} | 規則{finding.rule} | {finding.reason}"
        )
    if audit:
        lines.append("AUDIT: 非 owner-explicit 的現行決策")
        if report.audit_rows:
            for card in report.audit_rows:
                decision_key = card.fields.get(DECISION_KEY_FIELD, "").strip() or "<空白>"
                decided_by = card.fields.get(DECIDED_BY_FIELD, "").strip() or "<缺欄>"
                lines.append(
                    f"AUDIT: {card.path} | decision_key={decision_key} | decided_by={decided_by}"
                )
        else:
            lines.append("AUDIT: 無")
    lines.append(
        f"統計：掃描 {len(report.cards)} 張決策卡 / FAIL {len(report.failures)} / 警告 {len(report.warnings)}"
    )
    return "\n".join(lines)


def _card_text(decision_key, status, decided_at, decided_by, extra=""):
    lines = [
        FRONTMATTER_BOUNDARY,
        f"{DECISION_KEY_FIELD}: {decision_key}",
        f"{DECISION_STATUS_FIELD}: {status}",
        f"{CURRENT_DECISION_AT_FIELD}: {decided_at}",
        f"{DECIDED_BY_FIELD}: {decided_by}",
    ]
    if extra:
        lines.extend(extra.splitlines())
    lines.extend((FRONTMATTER_BOUNDARY, "測試卡"))
    return "\n".join(lines) + "\n"


def _selftest():
    checks = []
    with tempfile.TemporaryDirectory(prefix="decision-lint-") as temp_dir:
        root = Path(temp_dir).resolve()

        duplicate = root / "duplicate"
        duplicate.mkdir()
        for name in ("a.md", "b.md"):
            (duplicate / name).write_text(
                _card_text(
                    "same-key",
                    ACTIVE_DECISION_STATUS,
                    "2026-09-01",
                    OWNER_EXPLICIT_DECIDER,
                    f"{OWNER_QUOTE_FIELD}: 已裁定",
                ),
                encoding="utf-8",
            )
        result = lint_vault(duplicate)
        duplicate_failure = next(
            (item for item in result.failures if item.rule == "1" and "2 張現行卡" in item.reason),
            None,
        )
        checks.append(
            (
                "雙現行卡",
                duplicate_failure is not None
                and "a.md" in duplicate_failure.path_text
                and "b.md" in duplicate_failure.path_text,
            )
        )

        broken = root / "broken"
        broken.mkdir()
        (broken / "old.md").write_text(
            _card_text(
                "chain",
                SUPERSEDED_DECISION_STATUS,
                "2026-08-31",
                OWNER_EXPLICIT_DECIDER,
                f"{OWNER_QUOTE_FIELD}: 舊裁定\n{SUPERSEDED_BY_FIELD}: missing.md",
            ),
            encoding="utf-8",
        )
        result = lint_vault(broken)
        checks.append(("斷鏈 superseded_by", any(item.rule == "2" for item in result.failures)))

        missing_quote = root / "missing_quote"
        missing_quote.mkdir()
        (missing_quote / "card.md").write_text(
            _card_text(
                "quote",
                ACTIVE_DECISION_STATUS,
                "2026-09-01",
                OWNER_EXPLICIT_DECIDER,
            ),
            encoding="utf-8",
        )
        result = lint_vault(missing_quote)
        checks.append(
            (
                "owner-explicit 缺 owner_quote",
                any(item.rule == "3" and "owner_quote" in item.reason for item in result.failures),
            )
        )

        bad_date = root / "bad_date"
        bad_date.mkdir()
        (bad_date / "card.md").write_text(
            _card_text(
                "date",
                ACTIVE_DECISION_STATUS,
                "2026-99-99",
                OWNER_EXPLICIT_DECIDER,
                f"{OWNER_QUOTE_FIELD}: 已裁定",
            ),
            encoding="utf-8",
        )
        result = lint_vault(bad_date)
        checks.append(("壞日期", any(item.rule == "4" for item in result.failures)))

        good = root / "good"
        good.mkdir()
        (good / "current.md").write_text(
            _card_text(
                "good-chain",
                ACTIVE_DECISION_STATUS,
                "2026-09-01T08:30:00+08:00",
                OWNER_EXPLICIT_DECIDER,
                f"{OWNER_QUOTE_FIELD}: 採用現行版",
            ),
            encoding="utf-8",
        )
        (good / "old.md").write_text(
            _card_text(
                "good-chain",
                SUPERSEDED_DECISION_STATUS,
                "2026-08-31",
                OWNER_EXPLICIT_DECIDER,
                f"{OWNER_QUOTE_FIELD}: 取代舊版\n{SUPERSEDED_BY_FIELD}: current.md",
            ),
            encoding="utf-8",
        )
        result = lint_vault(good)
        checks.append(("全好卡組 exit 0", result.exit_code == 0))

        audit_vault = root / "audit"
        audit_vault.mkdir()
        (audit_vault / "ai.md").write_text(
            _card_text(
                "audit-key",
                ACTIVE_DECISION_STATUS,
                "2026-09-01",
                AI_AUTONOMOUS_DECIDER,
            ),
            encoding="utf-8",
        )
        result = lint_vault(audit_vault, audit=True)
        output = render_report(result, audit=True)
        checks.append(
            (
                "audit 列出 ai-autonomous",
                result.exit_code == 0
                and "ai.md" in output
                and "audit-key" in output
                and AI_AUTONOMOUS_DECIDER in output,
            )
        )

        bad_yaml = root / "bad_yaml"
        bad_yaml.mkdir()
        (bad_yaml / "bad.md").write_text(
            "---\ndecision_key: yaml\n這行壞掉\n---\n",
            encoding="utf-8",
        )
        result = lint_vault(bad_yaml)
        checks.append(("壞 YAML 警告後續掃", len(result.cards) == 1 and len(result.warnings) >= 1))

        no_current = root / "no_current"
        no_current.mkdir()
        (no_current / "old.md").write_text(
            _card_text(
                "old-only",
                SUPERSEDED_DECISION_STATUS,
                "2026-08-30",
                OWNER_EXPLICIT_DECIDER,
                f"{OWNER_QUOTE_FIELD}: 舊版\n{SUPERSEDED_BY_FIELD}: successor.md",
            ),
            encoding="utf-8",
        )
        (no_current / "successor.md").write_text(
            _card_text(
                "different-key",
                ACTIVE_DECISION_STATUS,
                "2026-09-01",
                OWNER_EXPLICIT_DECIDER,
                f"{OWNER_QUOTE_FIELD}: 另一決策",
            ),
            encoding="utf-8",
        )
        result = lint_vault(no_current)
        checks.append(
            (
                "零現行卡只警告，跨 key replacement 失敗",
                result.exit_code == 1
                and any(
                    item.rule == "1" and "沒有現行卡" in item.reason
                    for item in result.warnings
                )
                and any(
                    item.rule == "2" and "相同 decision_key" in item.reason
                    for item in result.failures
                ),
            )
        )

        stale_target = root / "stale_target"
        stale_target.mkdir()
        (stale_target / "current.md").write_text(
            _card_text(
                "replacement-key",
                ACTIVE_DECISION_STATUS,
                "2026-09-01",
                OWNER_EXPLICIT_DECIDER,
                f"{OWNER_QUOTE_FIELD}: 現行版",
            ),
            encoding="utf-8",
        )
        (stale_target / "middle.md").write_text(
            _card_text(
                "replacement-key",
                SUPERSEDED_DECISION_STATUS,
                "2026-08-31",
                OWNER_EXPLICIT_DECIDER,
                f"{OWNER_QUOTE_FIELD}: 中間版\n{SUPERSEDED_BY_FIELD}: current.md",
            ),
            encoding="utf-8",
        )
        (stale_target / "old.md").write_text(
            _card_text(
                "replacement-key",
                SUPERSEDED_DECISION_STATUS,
                "2026-08-30",
                OWNER_EXPLICIT_DECIDER,
                f"{OWNER_QUOTE_FIELD}: 舊版\n{SUPERSEDED_BY_FIELD}: middle.md",
            ),
            encoding="utf-8",
        )
        result = lint_vault(stale_target)
        checks.append((
            "old→middle→current 的歷史鏈是合法的",
            result.exit_code == 0 and not any(item.rule == "2" for item in result.failures),
        ))
        (stale_target / "current.md").write_text(
            _card_text(
                "replacement-key",
                SUPERSEDED_DECISION_STATUS,
                "2026-09-01",
                OWNER_EXPLICIT_DECIDER,
                f"{OWNER_QUOTE_FIELD}: 繞回舊版\n{SUPERSEDED_BY_FIELD}: old.md",
            ),
            encoding="utf-8",
        )
        result = lint_vault(stale_target)
        checks.append((
            "superseded_by 鏈形成循環時失敗",
            result.exit_code == 1
            and any(item.rule == "2" and "循環" in item.reason for item in result.failures),
        ))

        exact_path = root / "exact_path"
        (exact_path / "sub").mkdir(parents=True)
        (exact_path / "current.md").write_text(
            _card_text(
                "path-key",
                ACTIVE_DECISION_STATUS,
                "2026-09-01",
                OWNER_EXPLICIT_DECIDER,
                f"{OWNER_QUOTE_FIELD}: 根目錄現行卡",
            ),
            encoding="utf-8",
        )
        (exact_path / "old.md").write_text(
            _card_text(
                "path-key",
                SUPERSEDED_DECISION_STATUS,
                "2026-08-31",
                OWNER_EXPLICIT_DECIDER,
                f"{OWNER_QUOTE_FIELD}: 根目錄舊卡\n{SUPERSEDED_BY_FIELD}: current.md",
            ),
            encoding="utf-8",
        )
        (exact_path / "sub" / "current.md").write_text(
            _card_text(
                "other-key",
                ACTIVE_DECISION_STATUS,
                "2026-09-01",
                OWNER_EXPLICIT_DECIDER,
                f"{OWNER_QUOTE_FIELD}: 子目錄同名卡",
            ),
            encoding="utf-8",
        )
        result = lint_vault(exact_path)
        checks.append(("精確相對路徑優先", result.exit_code == 0))

        self_link = root / "self_link"
        self_link.mkdir()
        (self_link / "self.md").write_text(
            _card_text(
                "self-key",
                SUPERSEDED_DECISION_STATUS,
                "2026-08-31",
                OWNER_EXPLICIT_DECIDER,
                f"{OWNER_QUOTE_FIELD}: 自指測試\n{SUPERSEDED_BY_FIELD}: self.md",
            ),
            encoding="utf-8",
        )
        result = lint_vault(self_link)
        checks.append(
            (
                "拒絕 superseded_by 自指",
                any(item.rule == "2" and "不得指向自己" in item.reason for item in result.failures),
            )
        )

    passed = sum(bool(ok) for _, ok in checks)
    total = 12
    status = "PASS" if passed == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    if status != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="唯讀掃描 Epitype 決策卡")
    parser.add_argument("vault", nargs="?", help="要掃描的 vault 目錄")
    parser.add_argument("--audit", action="store_true", help="列出非 owner-explicit 的現行決策")
    parser.add_argument("--selftest", action="store_true", help="執行內建合成測試")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    if not args.vault:
        parser.error("必須提供 vault 目錄，或使用 --selftest")
    report = lint_vault(args.vault, audit=args.audit)
    print(render_report(report, audit=args.audit))
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
