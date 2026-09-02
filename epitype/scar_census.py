import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8，錯字元以 replacement 輸出。
"""Epitype 四層傷疤普查的常設機器生成視圖。"""

import argparse
from dataclasses import dataclass
from datetime import datetime
import io
import json
import os
from pathlib import Path
import re
import tempfile
from contextlib import redirect_stderr, redirect_stdout

from memspec import file_lock


# 2026-09-01 實測事故：傷疤散在常駐檔、索引紅標、教訓卡與工具冊四層，
# 未系統性盤點會讓記憶式摘要產生錯數；規則：摘要只能從檔案即時生成，
# 禁止手寫第二份副本。

FRONTMATTER_BOUNDARY = "---"
YAML_DOCUMENT_END = "..."
SUPPORTED_MODES = frozenset(("sections", "cards", "marked_lines"))
SECTION_HEADING = re.compile(r"^##[ \t]+(.+?)[ \t]*$")
FENCE_OPEN = re.compile(r"^[ ]{0,3}(?P<fence>`{3,}|~{3,}).*$")
YAML_FIELD = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*)\s*:\s*(.*)$")
GENERIC_VIEW_LAYER = re.compile(
    r"^## (?P<name>.+)（count=(?P<count>\d+), defects=(?P<defects>\d+)）$"
)
VIEW_LAYER_HEADING = "## {name}（count={count}, defects={defects}）"
VIEW_TOTAL_LABEL = "四層總計"
VIEW_GENERATED_MARKER = "機器生成,勿手改"
EXCERPT_MARKERS = ("出處", "事故")
LOCK_TIMEOUT_SECONDS = 5.0
EXPECTED_SELFTESTS = 8


class CensusError(ValueError):
    """設定、來源或既有視圖不能產生可信普查時的明確錯誤。"""


@dataclass(frozen=True)
class LayerSpec:
    name: str
    path: Path
    mode: str
    type_value: str | None = None
    marker: str | None = None


@dataclass(frozen=True)
class CensusEntry:
    name: str
    excerpt: str | None = None
    warning: str | None = None


@dataclass(frozen=True)
class CensusDefect:
    filename: str
    reason: str


@dataclass(frozen=True)
class LayerCensus:
    name: str
    entries: tuple[CensusEntry, ...]
    defects: tuple[CensusDefect, ...]

    @property
    def count(self):
        return len(self.entries)

    @property
    def defect_count(self):
        return len(self.defects)


def _one_line(text):
    """轉成單行但不截名字；清單項目不得因來源換行破版。"""
    return re.sub(r"\s+", " ", text).strip()


def _excerpt(text):
    if not any(marker in text for marker in EXCERPT_MARKERS):
        return None
    return _one_line(text)[:80]


def _defect(path, exc, *, filename=None):
    """把單項例外壓成穩定單行；清單同時保留檔名與原因。"""
    path = Path(path)
    reason = _one_line(str(exc)) or type(exc).__name__
    path_prefix = f"{path}:"
    read_prefix = f"無法以 UTF-8 讀取來源 {path}:"
    if reason.startswith(path_prefix):
        reason = reason[len(path_prefix) :].lstrip()
    elif reason.startswith(read_prefix):
        detail = reason[len(read_prefix) :].lstrip()
        reason = f"無法以 UTF-8 讀取：{detail}"
    return CensusDefect(filename=filename or path.name, reason=reason)


def _strip_inline_comment(value):
    """只為 frontmatter scalar 移除引號外的 YAML 行尾註解。"""
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


def _parse_scalar(raw_value, path, line_number):
    value = _strip_inline_comment(raw_value).strip()
    if not value:
        return ""
    if value[0] == "'":
        if len(value) < 2 or value[-1] != "'":
            raise CensusError(f"{path}:L{line_number} frontmatter 單引號未閉合")
        return value[1:-1].replace("''", "'")
    if value[0] == '"':
        if len(value) < 2 or value[-1] != '"':
            raise CensusError(f"{path}:L{line_number} frontmatter 雙引號未閉合")
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def _parse_flow_mapping(raw_value, path, line_number):
    """解析 metadata 的單層 flow mapping；遇到更深 YAML 結構就明確失敗。"""
    value = raw_value.strip()
    if not (value.startswith("{") and value.endswith("}")):
        raise CensusError(f"{path}:L{line_number} 不支援的 metadata 寫法")
    inner = value[1:-1].strip()
    if not inner:
        return {}

    parts = []
    start = 0
    quote = None
    escaped = False
    for index, character in enumerate(inner):
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
        if quote is None and character in "{}[]":
            raise CensusError(f"{path}:L{line_number} metadata flow mapping 不接受巢狀值")
        if quote is None and character == ",":
            parts.append(inner[start:index])
            start = index + 1
    if quote is not None:
        raise CensusError(f"{path}:L{line_number} metadata flow mapping 引號未閉合")
    parts.append(inner[start:])

    fields = {}
    for part in parts:
        match = YAML_FIELD.match(part.strip())
        if match is None:
            raise CensusError(f"{path}:L{line_number} metadata flow mapping 格式錯誤")
        key, field_value = match.groups()
        if key in fields:
            raise CensusError(f"{path}:L{line_number} metadata flow mapping 重複欄位 {key}")
        fields[key] = _parse_scalar(field_value, path, line_number)
    return fields


def _frontmatter_fields(path):
    """解析 census 所需的 name 與 metadata.type；不假裝支援完整 YAML。"""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise CensusError(f"無法以 UTF-8 讀取卡片 {path}: {type(exc).__name__}") from exc

    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER_BOUNDARY:
        return {}, text

    closing_index = next(
        (
            index
            for index in range(1, len(lines))
            if lines[index].strip() in (FRONTMATTER_BOUNDARY, YAML_DOCUMENT_END)
        ),
        None,
    )
    if closing_index is None:
        raise CensusError(f"{path}: frontmatter 缺少結束界線")

    fields = {}
    metadata_indent = None
    metadata_child_indent = None
    for line_number, raw_line in enumerate(lines[1:closing_index], start=2):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        leading = raw_line[: len(raw_line) - len(raw_line.lstrip())]
        if "\t" in leading:
            raise CensusError(f"{path}:L{line_number} frontmatter 不接受 tab 縮排")
        indent = len(leading)
        match = YAML_FIELD.match(raw_line.lstrip())
        if match is None:
            continue
        key, raw_value = match.groups()

        if indent == 0:
            metadata_indent = None
            metadata_child_indent = None
            if key == "metadata":
                metadata_value = _strip_inline_comment(raw_value).strip()
                if not metadata_value:
                    metadata_indent = 0
                else:
                    metadata = _parse_flow_mapping(metadata_value, path, line_number)
                    if "type" in metadata:
                        if "metadata.type" in fields:
                            raise CensusError(
                                f"{path}:L{line_number} frontmatter 重複欄位 metadata.type"
                            )
                        fields["metadata.type"] = metadata["type"]
                continue
            if key in ("name", "metadata.type"):
                if key in fields:
                    raise CensusError(f"{path}:L{line_number} frontmatter 重複欄位 {key}")
                fields[key] = _parse_scalar(raw_value, path, line_number)
            continue

        if metadata_indent is not None and indent > metadata_indent:
            if metadata_child_indent is None:
                metadata_child_indent = indent
            if indent != metadata_child_indent or key != "type":
                continue
            field_name = "metadata.type"
            if field_name in fields:
                raise CensusError(
                    f"{path}:L{line_number} frontmatter 重複欄位 {field_name}"
                )
            fields[field_name] = _parse_scalar(raw_value, path, line_number)

    body = "\n".join(lines[closing_index + 1 :])
    return fields, body


def _config_string(layer, key, index, *, optional=False):
    value = layer.get(key)
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise CensusError(f"layers[{index}].{key} 必須是非空字串")
    return value.strip()


def load_config(config_path):
    config_path = Path(config_path).expanduser().resolve()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise CensusError(f"設定檔不存在：{config_path}") from exc
    except UnicodeError as exc:
        raise CensusError(f"設定檔不是 UTF-8：{config_path}") from exc
    except json.JSONDecodeError as exc:
        raise CensusError(f"設定檔 JSON 錯誤：L{exc.lineno} C{exc.colno}") from exc

    if not isinstance(raw, dict) or not isinstance(raw.get("layers"), list):
        raise CensusError("設定檔根物件必須包含 layers 陣列")
    if not raw["layers"]:
        raise CensusError("layers 陣列不得為空")

    specs = []
    seen_names = set()
    for index, layer in enumerate(raw["layers"]):
        if not isinstance(layer, dict):
            raise CensusError(f"layers[{index}] 必須是物件")
        name = _config_string(layer, "name", index)
        if name in seen_names:
            raise CensusError(f"層名不得重複：{name}")
        seen_names.add(name)

        raw_path = _config_string(layer, "path", index)
        source_path = Path(raw_path).expanduser()
        if not source_path.is_absolute():
            source_path = config_path.parent / source_path
        source_path = source_path.resolve()

        mode = _config_string(layer, "mode", index)
        if mode not in SUPPORTED_MODES:
            choices = ", ".join(sorted(SUPPORTED_MODES))
            raise CensusError(f"layers[{index}].mode 必須是 {choices} 之一")

        type_value = None
        marker = None
        if mode == "cards":
            raw_type = layer.get("type", layer.get("type_value"))
            if not isinstance(raw_type, str) or not raw_type.strip():
                raise CensusError(f"layers[{index}] cards 模式必須提供非空 type")
            type_value = raw_type.strip()
        elif mode == "marked_lines":
            marker = _config_string(layer, "marker", index)

        specs.append(
            LayerSpec(
                name=name,
                path=source_path,
                mode=mode,
                type_value=type_value,
                marker=marker,
            )
        )
    return config_path, tuple(specs)


def _read_source(path, expected_kind):
    if expected_kind == "file" and not path.is_file():
        raise CensusError(f"來源不是檔案或不存在：{path}")
    if expected_kind == "directory" and not path.is_dir():
        raise CensusError(f"來源不是目錄或不存在：{path}")
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise CensusError(f"無法以 UTF-8 讀取來源 {path}: {type(exc).__name__}") from exc


def _count_sections(spec):
    try:
        text = _read_source(spec.path, "file")
    except Exception as exc:
        return (), (_defect(spec.path, exc),)

    lines = text.splitlines()
    headings = []
    defects = []
    active_fence = None
    for index, line in enumerate(lines):
        try:
            if active_fence is not None:
                fence_character, fence_length = active_fence
                closing = re.compile(
                    rf"^[ ]{{0,3}}{re.escape(fence_character)}{{{fence_length},}}[ \t]*$"
                )
                if closing.match(line):
                    active_fence = None
                continue
            fence_match = FENCE_OPEN.match(line)
            if fence_match is not None:
                fence = fence_match.group("fence")
                active_fence = (fence[0], len(fence))
                continue
            match = SECTION_HEADING.match(line)
            if match is None:
                continue
            raw_name = re.sub(r"[ \t]+#+[ \t]*$", "", match.group(1)).strip()
            name = _one_line(raw_name)
            if not name:
                raise CensusError("H2 標題不得空白")
            headings.append((index, name))
        except Exception as exc:
            defects.append(
                _defect(spec.path, exc, filename=f"{spec.path.name}:L{index + 1}")
            )

    entries = []
    for offset, (start, name) in enumerate(headings):
        try:
            end = headings[offset + 1][0] if offset + 1 < len(headings) else len(lines)
            section_text = "\n".join(lines[start:end])
            entries.append(CensusEntry(name=name, excerpt=_excerpt(section_text)))
        except Exception as exc:
            defects.append(
                _defect(spec.path, exc, filename=f"{spec.path.name}:L{start + 1}")
            )
    return tuple(entries), tuple(defects)


def _count_cards(spec):
    if not spec.path.is_dir():
        raise CensusError(f"來源不是目錄或不存在：{spec.path}")
    try:
        paths = sorted(spec.path.rglob("*.md"), key=lambda item: str(item).casefold())
    except Exception as exc:
        return (), (_defect(spec.path, exc),)

    entries = []
    defects = []
    for path in paths:
        filename = path.name
        try:
            filename = path.relative_to(spec.path).as_posix()
            fields, body = _frontmatter_fields(path)
            if fields.get("metadata.type", "").strip() != spec.type_value:
                continue
            name = _one_line(fields.get("name", ""))
            warning = None
            if not name:
                name = path.stem
                warning = "⚠缺 name 欄"
                defects.append(CensusDefect(filename=filename, reason="缺 name 欄"))
            entries.append(
                CensusEntry(name=name, excerpt=_excerpt(body), warning=warning)
            )
        except Exception as exc:
            defects.append(_defect(path, exc, filename=filename))
    return tuple(entries), tuple(defects)


def _count_marked_lines(spec):
    try:
        text = _read_source(spec.path, "file")
    except Exception as exc:
        return (), (_defect(spec.path, exc),)

    entries = []
    defects = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        try:
            if spec.marker not in line:
                continue
            entries.append(CensusEntry(name=line[:60], excerpt=_excerpt(line)))
        except Exception as exc:
            defects.append(
                _defect(spec.path, exc, filename=f"{spec.path.name}:L{line_number}")
            )
    return tuple(entries), tuple(defects)


def count_layers(specs):
    counters = {
        "sections": _count_sections,
        "cards": _count_cards,
        "marked_lines": _count_marked_lines,
    }
    layers = []
    for spec in specs:
        try:
            entries, defects = counters[spec.mode](spec)
        except Exception as exc:
            entries = ()
            defects = (_defect(spec.path, exc),)
        layers.append(LayerCensus(name=spec.name, entries=entries, defects=defects))
    return tuple(layers)


def _render_layer_lines(layer):
    lines = [
        VIEW_LAYER_HEADING.format(
            name=layer.name,
            count=layer.count,
            defects=layer.defect_count,
        )
    ]
    for entry in layer.entries:
        item = f"- {entry.name}"
        if entry.excerpt is not None:
            item += f"｜出處或事故前80字：{entry.excerpt}"
        if entry.warning is not None:
            item += f" {entry.warning}"
        lines.append(item)
    if not layer.entries:
        lines.append("- （無）")
    for defect in layer.defects:
        lines.append(f"- ⚠清單：{defect.filename}｜{defect.reason}")
    return lines


def render_view(config_path, layers, generated_at=None):
    generated_at = generated_at or datetime.now().astimezone().isoformat(timespec="seconds")
    lines = [
        f"產生時間：{generated_at}｜設定檔：{Path(config_path).resolve()}｜{VIEW_GENERATED_MARKER}",
        "",
    ]
    total = 0
    for layer in layers:
        total += layer.count
        lines.extend(_render_layer_lines(layer))
        lines.append("")
    lines.append(f"{VIEW_TOTAL_LABEL}：{total}")
    return "\n".join(lines) + "\n"


def _atomic_locked_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path, LOCK_TIMEOUT_SECONDS) as acquired:
        if not acquired:
            raise CensusError(f"無法取得輸出鎖：{path}")
        file_descriptor = None
        temporary_path = None
        try:
            file_descriptor, raw_path = tempfile.mkstemp(
                prefix=f".{path.name}.tmp-",
                dir=path.parent,
            )
            temporary_path = Path(raw_path)
            with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as stream:
                file_descriptor = None
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass


def build_view(config, out):
    config_path, specs = load_config(config)
    layers = count_layers(specs)
    payload = render_view(config_path, layers)
    _atomic_locked_write(Path(out).expanduser().resolve(), payload)
    return payload, layers


def _view_stats(view_text, layer_name):
    pattern = re.compile(
        rf"^## {re.escape(layer_name)}（count=(?P<count>\d+), defects=(?P<defects>\d+)）$",
        re.MULTILINE,
    )
    matches = list(pattern.finditer(view_text))
    if len(matches) != 1:
        return None, None
    return int(matches[0].group("count")), int(matches[0].group("defects"))


def _view_count(view_text, layer_name):
    return _view_stats(view_text, layer_name)[0]


def _view_defects(view_text, layer_name):
    return _view_stats(view_text, layer_name)[1]


def _view_layer_lines(view_text, layer_name):
    lines = view_text.splitlines()
    pattern = re.compile(
        rf"^## {re.escape(layer_name)}（count=\d+, defects=\d+）$"
    )
    starts = [index for index, line in enumerate(lines) if pattern.match(line)]
    if len(starts) != 1:
        return None
    start = starts[0]
    end = start + 1
    while end < len(lines):
        if GENERIC_VIEW_LAYER.match(lines[end]) or lines[end].startswith(
            f"{VIEW_TOTAL_LABEL}："
        ):
            break
        end += 1
    while end > start and not lines[end - 1]:
        end -= 1
    return lines[start:end]


def _view_total(view_text):
    pattern = re.compile(rf"^{re.escape(VIEW_TOTAL_LABEL)}：(?P<count>\d+)$", re.MULTILINE)
    matches = list(pattern.finditer(view_text))
    if len(matches) != 1:
        return None
    return int(matches[0].group("count"))


def check_view(config, out):
    config_path, specs = load_config(config)
    del config_path
    layers = count_layers(specs)
    out_path = Path(out).expanduser().resolve()
    try:
        view_text = out_path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        print(f"DRIFT 視圖不存在：{out_path}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError) as exc:
        print(f"DRIFT 視圖無法讀取：{out_path} | {type(exc).__name__}", file=sys.stderr)
        return 1

    drifted = False
    for layer in layers:
        old_count = _view_count(view_text, layer.name)
        old_defects = _view_defects(view_text, layer.name)
        old_lines = _view_layer_lines(view_text, layer.name)
        expected_lines = _render_layer_lines(layer)
        if old_lines != expected_lines:
            details = []
            if old_count != layer.count:
                shown = "缺失或重複" if old_count is None else str(old_count)
                details.append(f"數量：視圖={shown}，現數={layer.count}")
            if old_defects != layer.defect_count:
                shown = "缺失或重複" if old_defects is None else str(old_defects)
                details.append(
                    f"defects：視圖={shown}，現數={layer.defect_count}"
                )
            if not details:
                details.append(
                    f"數量同為 {layer.count}、defects 同為 {layer.defect_count}，"
                    "但名字或出處已漂移"
                )
            print(
                f"DRIFT {layer.name}：{'；'.join(details)}",
                file=sys.stderr,
            )
            drifted = True

    expected_names = [layer.name for layer in layers]
    view_names = [
        match.group("name")
        for line in view_text.splitlines()
        if (match := GENERIC_VIEW_LAYER.match(line)) is not None
    ]
    if view_names != expected_names:
        first_difference = next(
            (
                actual
                for actual, expected in zip(view_names, expected_names)
                if actual != expected
            ),
            None,
        )
        shown = first_difference or next(
            (name for name in view_names if name not in expected_names),
            "視圖層順序",
        )
        print(f"DRIFT {shown}：層集合或順序與設定檔不同", file=sys.stderr)
        drifted = True

    current_total = sum(layer.count for layer in layers)
    old_total = _view_total(view_text)
    if old_total != current_total:
        shown = "缺失或重複" if old_total is None else str(old_total)
        print(
            f"DRIFT {VIEW_TOTAL_LABEL}：視圖={shown}，現數={current_total}",
            file=sys.stderr,
        )
        drifted = True
    expected_body = render_view("check", layers, generated_at="check").splitlines()[1:]
    actual_body = view_text.splitlines()[1:]
    if not drifted and actual_body != expected_body:
        print("DRIFT 視圖格式：時間行以外內容不符機器正本", file=sys.stderr)
        drifted = True
    if drifted:
        return 1
    current_defects = sum(layer.defect_count for layer in layers)
    print(
        f"CHECK PASS {len(layers)} 層 / {current_total} 條 / defects={current_defects}"
    )
    return 0


def _card_text(name, type_value, body, *, style="block"):
    type_lines = {
        "block": ["metadata:", f"  type: {type_value}"],
        "dotted": [f"metadata.type: {type_value}"],
        "flow": [f"metadata: {{type: {type_value}}}"],
    }[style]
    return "\n".join(
        [FRONTMATTER_BOUNDARY, f"name: {name}", *type_lines, FRONTMATTER_BOUNDARY, body, ""]
    )


def _selftest():
    checks = []
    try:
        with tempfile.TemporaryDirectory(prefix="scar-census-") as temp_dir:
            root = Path(temp_dir).resolve()
            sections_path = root / "resident.md"
            cards_path = root / "cards"
            marked_path = root / "index.md"
            tools_path = root / "tools.md"
            config_path = root / "config.json"
            out_path = root / "scar_census.md"
            cards_path.mkdir()

            chinese_name = "中文傷疤名字完整保留"
            english_name = "English Scar Name Remains Whole"
            sections_text = "\n".join(
                (
                    "# 合成常駐檔",
                    f"## {chinese_name}",
                    "事故出處：這是合成資料，不是真實 vault。",
                    "### 子標題不另計",
                    "```markdown",
                    "## fenced heading 不得計入",
                    "```",
                    f"## {english_name}",
                    "ordinary body",
                    "",
                )
            )
            sections_path.write_text(sections_text, encoding="utf-8")
            long_excerpt = "事故" + "中" * 78 + "尾端不應出現在前80字"
            (cards_path / "one.md").write_text(
                _card_text("中文教訓卡", "scar", long_excerpt),
                encoding="utf-8",
            )
            nested = cards_path / "nested"
            nested.mkdir()
            (nested / "two.md").write_text(
                _card_text("English lesson card", "scar", "plain body", style="dotted"),
                encoding="utf-8",
            )
            (cards_path / "flow.md").write_text(
                _card_text("Flow metadata card", "scar", "plain body", style="flow"),
                encoding="utf-8",
            )
            (cards_path / "ignored.md").write_text(
                _card_text("不應計入", "note", "事故但 type 不符。"),
                encoding="utf-8",
            )
            boundary_line = "E" * 59 + "中" + "尾 [SCAR]"
            marked_path.write_text(
                f"事故紅標 中文 [SCAR]\nEnglish marked line [SCAR]\n{boundary_line}\nno marker\n",
                encoding="utf-8",
            )
            tools_path.write_text("工具冊事故 [TOOL-SCAR]\n", encoding="utf-8")
            config = {
                "layers": [
                    {"name": "常駐傷疤檔", "path": "resident.md", "mode": "sections"},
                    {"name": "教訓卡", "path": "cards", "mode": "cards", "type": "scar"},
                    {"name": "索引紅標", "path": "index.md", "mode": "marked_lines", "marker": "[SCAR]"},
                    {"name": "工具冊", "path": "tools.md", "mode": "marked_lines", "marker": "[TOOL-SCAR]"},
                ]
            }
            config_path.write_text(
                json.dumps(config, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            first_payload, first_layers = build_view(config_path, out_path)
            counts = {layer.name: layer.count for layer in first_layers}
            checks.append(("sections 數對", counts.get("常駐傷疤檔") == 2))
            checks.append(("cards 依 type 過濾數對", counts.get("教訓卡") == 3))
            checks.append(("marked_lines 數對", counts.get("索引紅標") == 3))

            second_payload, _ = build_view(config_path, out_path)
            first_body = first_payload.splitlines()[1:]
            second_body = second_payload.splitlines()[1:]
            checks.append(
                (
                    "時間行外冪等",
                    first_body == second_body
                    and out_path.read_text(encoding="utf-8") == second_payload,
                )
            )

            checks.append(
                (
                    "中英名字不截壞",
                    f"- {chinese_name}" in second_payload
                    and f"- {english_name}" in second_payload
                    and f"- {boundary_line[:60]}\n" in second_payload
                    and f"出處或事故前80字：{long_excerpt[:80]}" in second_payload,
                )
            )

            sections_path.write_text(
                sections_text.replace(english_name, english_name + " renamed"),
                encoding="utf-8",
            )
            same_count_out = io.StringIO()
            same_count_err = io.StringIO()
            with redirect_stdout(same_count_out), redirect_stderr(same_count_err):
                same_count_code = check_view(config_path, out_path)
            sections_path.write_text(sections_text, encoding="utf-8")

            marked_path.write_text(
                marked_path.read_text(encoding="utf-8") + "新增漂移 [SCAR]\n",
                encoding="utf-8",
            )
            captured_out = io.StringIO()
            captured_err = io.StringIO()
            with redirect_stdout(captured_out), redirect_stderr(captured_err):
                drift_code = check_view(config_path, out_path)
            checks.append(
                (
                    "--check 抓漂移",
                    same_count_code == 1
                    and "DRIFT 常駐傷疤檔" in same_count_err.getvalue()
                    and "名字或出處已漂移" in same_count_err.getvalue()
                    and drift_code == 1
                    and "DRIFT 索引紅標" in captured_err.getvalue()
                    and "現數=4" in captured_err.getvalue(),
                )
            )

            missing_name_path = cards_path / "missing-name.md"
            missing_name_path.write_text(
                "\n".join(
                    (
                        FRONTMATTER_BOUNDARY,
                        "metadata:",
                        "  type: scar",
                        FRONTMATTER_BOUNDARY,
                        "缺名卡仍應計入。",
                        "",
                    )
                ),
                encoding="utf-8",
            )
            missing_payload, missing_layers = build_view(config_path, out_path)
            missing_cards = next(
                layer for layer in missing_layers if layer.name == "教訓卡"
            )
            checks.append(
                (
                    "缺 name 卡 fallback 檔名並標記",
                    missing_cards.count == 4
                    and missing_cards.defect_count == 1
                    and "- missing-name ⚠缺 name 欄" in missing_payload
                    and "- ⚠清單：missing-name.md｜缺 name 欄" in missing_payload,
                )
            )

            bad_frontmatter_path = cards_path / "bad-frontmatter.md"
            bad_frontmatter_path.write_text(
                "\n".join(
                    (
                        FRONTMATTER_BOUNDARY,
                        "name: 不得計入的壞卡",
                        "metadata:",
                        "  type: scar",
                        "缺少 frontmatter 結束界線",
                        "",
                    )
                ),
                encoding="utf-8",
            )
            defect_check_out = io.StringIO()
            defect_check_err = io.StringIO()
            with redirect_stdout(defect_check_out), redirect_stderr(defect_check_err):
                defect_drift_code = check_view(config_path, out_path)
            bad_payload, bad_layers = build_view(config_path, out_path)
            bad_cards = next(layer for layer in bad_layers if layer.name == "教訓卡")
            checks.append(
                (
                    "壞 frontmatter 卡跳過並計 defects",
                    defect_drift_code == 1
                    and "DRIFT 教訓卡" in defect_check_err.getvalue()
                    and "defects：視圖=1，現數=2" in defect_check_err.getvalue()
                    and bad_cards.count == 4
                    and bad_cards.defect_count == 2
                    and all(entry.name != "不得計入的壞卡" for entry in bad_cards.entries)
                    and "- ⚠清單：bad-frontmatter.md｜frontmatter 缺少結束界線"
                    in bad_payload,
                )
            )
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = sum(bool(ok) for _, ok in checks)
    status = (
        "PASS"
        if passed == EXPECTED_SELFTESTS and len(checks) == EXPECTED_SELFTESTS
        else "FAIL"
    )
    print(f"SELFTEST {status} {passed}/{EXPECTED_SELFTESTS}")
    if status != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


def _parser():
    parser = argparse.ArgumentParser(description="建立 Epitype 四層傷疤普查機器視圖")
    parser.add_argument("--selftest", action="store_true", help="只用合成資料執行內建測試")
    commands = parser.add_subparsers(dest="command")
    build = commands.add_parser("build", help="建立視圖，或檢查既有視圖數字")
    build.add_argument("--config", required=True, help="四層 JSON 設定檔")
    build.add_argument("--out", required=True, help="Markdown 視圖輸出路徑")
    build.add_argument("--check", action="store_true", help="不寫檔，只比對視圖與現數")
    return parser


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    if args.command != "build":
        parser.print_help(sys.stderr)
        return 2
    try:
        if args.check:
            return check_view(args.config, args.out)
        build_view(args.config, args.out)
    except (CensusError, OSError) as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
