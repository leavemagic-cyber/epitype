import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8；唯讀/寫入 CLI 不落 pyc。
"""Epitype 核心生成器：規則卡（`type: rule`）→ 常駐核心塊。

核心記憶＝手寫底線＋常駐規則短句，而那些短句由卡片生成，不由人各寫一份（owner
2026-09-09 最終方案）。這支工具只做三件事：**挑**（哪些卡屬於生成的兩層）、**排**
（floor 依 order 編號、resident 依 section 分小節）、**抄**（`text` 逐位元組照搬）。

它刻意不會的事：不改寫任何一張卡的字（改寫等於在核准之外又生出一份規則）、不生成
標題與說明以外的任何句子（產品不內建行為守則文字，FAILURE_MODES §30）、不寫任何
契約檔或宿主檔（那是本機作業，不是產品）。超過上限、或有卡沒有核准憑證時，一個
位元組都不寫出去——寧可沒有核心塊，也不要一份沒人核過的核心塊。

`--check` 是同一條組裝路徑的比對版：重組一次，與現有檔逐位元組比。給本機的漂移
稽核與夢的第 11 節用，兩邊看到的「漂移」才會是同一個定義。
"""

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile

try:
    from . import card_lint, memsearch, memspec
except ImportError:  # Direct script execution keeps the CLI contract.
    import card_lint
    import memsearch
    import memspec

STATUS_WRITTEN = "written"
STATUS_UNCHANGED = "unchanged"
STATUS_DRY_RUN = "dry-run"
STATUS_OVER_CAP = "over-cap"
STATUS_UNAPPROVED = "unapproved"
STATUS_MATCH = "match"
STATUS_DRIFT = "drift"
STATUS_UNREADABLE = "unreadable"

# 拒絕（超上限、缺核准、漂移）是 1；工具自己壞掉是 2。呼叫端要分得出「產品說不行」
# 與「產品跑不起來」。
EXIT_REFUSED = 1
EXIT_ERROR = 2


def _order_of(fields):
    """排序鍵；沒寫或寫壞（card_lint 判 FAIL）就排到最後，而不是讓生成整個失敗。"""
    try:
        return int(fields.get(memspec.RULE_ORDER_FIELD, "").strip())
    except (AttributeError, TypeError, ValueError):
        return None


def _sort_key(rule):
    return (rule["order"] is None, rule["order"] or 0, rule["vault"], rule["path"])


def collect_rules(vaults):
    """(規則卡清單, 讀不到的東西)。

    掃描範圍與型別判定都不自己造一套：範圍用 `memsearch.scan_cards`、型別用
    `card_lint.card_type_of`，否則同一張卡會在 lint 裡是規則卡、在生成器裡不是。
    `status: superseded` 的卡不進生成——取代鏈語意與決策卡同一套。
    """
    rules = []
    errors = []
    for raw in vaults:
        vault = Path(raw).expanduser().resolve()
        try:
            scanned = memsearch.scan_cards(vault)
        except OSError as exc:
            errors.append(f"{vault}: {type(exc).__name__}: {exc}")
            continue
        for relative, path, _mtime_ns, _size in scanned:
            try:
                text = path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError) as exc:
                errors.append(f"{vault}｜{relative}: {type(exc).__name__}: {exc}")
                continue
            card_type, fields = card_lint.card_type_of(relative, text, path)
            if card_type != memspec.CARD_TYPE_RULE:
                continue
            status = fields.get(memspec.DECISION_STATUS_FIELD, "").strip()
            if status == memspec.SUPERSEDED_DECISION_STATUS:
                continue
            rules.append({
                "vault": str(vault),
                "path": relative,
                "layer": fields.get(memspec.RULE_LAYER_FIELD, "").strip(),
                "section": fields.get(memspec.RULE_SECTION_FIELD, "").strip(),
                "order": _order_of(fields),
                "text": fields.get(memspec.RULE_TEXT_FIELD, ""),
                "approved_by": fields.get(memspec.RULE_APPROVED_BY_FIELD, "").strip(),
                "approved_at": fields.get(memspec.RULE_APPROVED_AT_FIELD, "").strip(),
            })
    rules.sort(key=_sort_key)
    return rules, errors


def _of_layer(rules, layer):
    return [rule for rule in rules if rule["layer"] == layer]


def _resident_sections(resident):
    """[(section, 該節的卡)]；節的先後由節內最小 order 決定（清單已排好序）。"""
    groups = {}
    for rule in resident:
        groups.setdefault(rule["section"], []).append(rule)
    return list(groups.items())


def unapproved(rules):
    """生成兩層裡缺核准憑證的卡。核准是生成的前提，不是事後補的欄位。"""
    return [
        rule for rule in rules
        if rule["layer"] in memspec.RULE_GENERATED_LAYERS
        and not (rule["approved_by"] and rule["approved_at"])
    ]


def assemble(rules):
    """核心塊的完整文字。`text` 逐位元組照抄，其餘每一行都來自 memspec 的模板。"""
    floor = _of_layer(rules, memspec.RULE_LAYER_FLOOR)
    resident = _of_layer(rules, memspec.RULE_LAYER_RESIDENT)
    lines = [
        memspec.CORE_GEN_OUTPUT_TITLE,
        memspec.CORE_GEN_OUTPUT_NOTE.format(floor=len(floor), resident=len(resident)),
        "",
    ]
    if floor:
        lines.append(memspec.CORE_GEN_FLOOR_HEADING)
        lines.extend(
            memspec.CORE_GEN_FLOOR_LINE.format(number=number, text=rule["text"])
            for number, rule in enumerate(floor, start=1)
        )
        lines.append("")
    if resident:
        lines.append(memspec.CORE_GEN_RESIDENT_HEADING)
        for section, members in _resident_sections(resident):
            lines.append(memspec.CORE_GEN_SECTION_HEADING.format(section=section))
            lines.extend(
                memspec.CORE_GEN_RESIDENT_LINE.format(text=rule["text"]) for rule in members
            )
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _sha256(payload):
    return hashlib.sha256(payload).hexdigest()


def _cap_of(cap_bytes, config):
    """`--cap-bytes` 優先，其次設定的 `core_cap_bytes`；都沒有就不擋。

    產品不猜上限（FAILURE_MODES §36）：沒設就是沒設，不套一個內建數字再說它超標。
    """
    if cap_bytes is not None:
        return int(cap_bytes)
    value = (config or {}).get(memspec.CONFIG_CORE_CAP_BYTES_FIELD)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _longest(rules, limit=memspec.CORE_GEN_LONGEST_LISTED):
    ranked = sorted(
        (rule for rule in rules if rule["layer"] in memspec.RULE_GENERATED_LAYERS),
        key=lambda rule: (-len(rule["text"].encode("utf-8")), rule["vault"], rule["path"]),
    )
    return [
        {
            "vault": rule["vault"],
            "path": rule["path"],
            "layer": rule["layer"],
            "bytes": len(rule["text"].encode("utf-8")),
        }
        for rule in ranked[:limit]
    ]


def _pack(rules, out_path, payload, cap):
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "out": os.fspath(out_path),
        "out_sha256": _sha256(payload),
        "out_bytes": len(payload),
        "cap_bytes": cap,
        "cards": [
            {
                "vault": rule["vault"],
                "path": rule["path"],
                "layer": rule["layer"],
                "section": rule["section"],
                "order": rule["order"],
                "approved_by": rule["approved_by"],
                "approved_at": rule["approved_at"],
                "text_sha256": _sha256(rule["text"].encode("utf-8")),
            }
            for rule in rules
            if rule["layer"] in memspec.RULE_GENERATED_LAYERS
        ],
    }


def pack_path(vault):
    return Path(vault) / memspec.FTS_INDEX_DIRECTORY / memspec.CORE_GEN_PACK_FILENAME


def _write(path, payload):
    """先寫旁邊再換名：截斷式寫入被砍在中間會留下半份核心塊，而讀者無從分辨。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, staging = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, path)
    finally:
        Path(staging).unlink(missing_ok=True)


def generate(vaults, out, cap_bytes=None, dry_run=False, config=None):
    """組裝並（非 dry-run 時）寫出核心塊與核准包。回一份可直接印的結果。"""
    out_path = Path(out).expanduser()
    rules, errors = collect_rules(vaults)
    cap = _cap_of(cap_bytes, config if config is not None else memspec.config_options())
    text = assemble(rules)
    payload = text.encode("utf-8")
    result = {
        "status": STATUS_WRITTEN,
        "out": os.fspath(out_path),
        "bytes": len(payload),
        "cap": cap,
        "rules": len(rules),
        "floor": len(_of_layer(rules, memspec.RULE_LAYER_FLOOR)),
        "resident": len(_of_layer(rules, memspec.RULE_LAYER_RESIDENT)),
        "skipped": sum(
            1 for rule in rules if rule["layer"] not in memspec.RULE_GENERATED_LAYERS
        ),
        "errors": errors,
        "offenders": [],
        "pack": None,
        "text": text,
    }

    missing = unapproved(rules)
    if missing:
        result["status"] = STATUS_UNAPPROVED
        result["offenders"] = [
            {"vault": rule["vault"], "path": rule["path"], "layer": rule["layer"],
             "approved_by": rule["approved_by"], "approved_at": rule["approved_at"]}
            for rule in missing
        ]
        return result
    if cap is not None and len(payload) > cap:
        result["status"] = STATUS_OVER_CAP
        result["offenders"] = _longest(rules)
        return result
    if dry_run:
        result["status"] = STATUS_DRY_RUN
        return result

    try:
        unchanged = out_path.is_file() and out_path.read_bytes() == payload
    except OSError:
        unchanged = False
    if not unchanged:
        _write(out_path, payload)
    result["status"] = STATUS_UNCHANGED if unchanged else STATUS_WRITTEN

    pack = _pack(rules, out_path, payload, cap)
    target = pack_path(Path(vaults[0]).expanduser().resolve())
    try:
        _write(target, (json.dumps(pack, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
        result["pack"] = os.fspath(target)
    except OSError as exc:
        result["errors"] = list(errors) + [f"{target}: {type(exc).__name__}: {exc}"]
    return result


def check(vaults, out):
    """重組一次並與現有檔逐位元組比對。不寫任何檔案。"""
    out_path = Path(out).expanduser()
    rules, errors = collect_rules(vaults)
    payload = assemble(rules).encode("utf-8")
    result = {
        "status": STATUS_MATCH,
        "out": os.fspath(out_path),
        "bytes": len(payload),
        "rules": len(rules),
        "errors": errors,
        "reason": None,
    }
    try:
        current = out_path.read_bytes()
    except OSError as exc:
        result["status"] = STATUS_UNREADABLE
        result["reason"] = memspec.CORE_GEN_MISSING_REASON.format(
            out=os.fspath(out_path), error=f"{type(exc).__name__}: {exc}"
        )
        return result
    if current != payload:
        result["status"] = STATUS_DRIFT
        result["reason"] = memspec.CORE_GEN_DRIFT_REASON.format(out=os.fspath(out_path))
    return result


def drifted(vaults, out):
    """夢的第 11 節要的一句話：True＝漂移、False＝一致、None＝這次無從判斷。

    「無從判斷」包含兩種：讀不到那個檔，以及庫裡一張規則卡都沒有——沒有規則卡就沒有
    生成塊可比，把「空的組裝 vs 有內容的檔」報成漂移只是每晚固定的一則噪音。
    """
    rules, _errors = collect_rules(vaults)
    if not rules:
        return None
    result = check(vaults, out)
    if result["status"] == STATUS_UNREADABLE:
        return None
    return result["status"] == STATUS_DRIFT


def _print(result, output):
    for error in result.get("errors") or ():
        print(f"（略過：{error}）", file=output)
    status = result["status"]
    if status == STATUS_UNAPPROVED:
        print(
            memspec.CORE_GEN_UNAPPROVED_REASON.format(
                count=len(result["offenders"]),
                layers="／".join(memspec.RULE_GENERATED_LAYERS),
                fields="／".join(
                    (memspec.RULE_APPROVED_BY_FIELD, memspec.RULE_APPROVED_AT_FIELD)
                ),
            ),
            file=output,
        )
        for item in result["offenders"]:
            print(f"- {item['layer']}｜{item['path']}｜{item['vault']}", file=output)
    elif status == STATUS_OVER_CAP:
        print(
            memspec.CORE_GEN_OVER_CAP_REASON.format(
                bytes=result["bytes"], cap=result["cap"], over=result["bytes"] - result["cap"]
            ),
            file=output,
        )
        for item in result["offenders"]:
            print(f"- {item['bytes']} bytes｜{item['layer']}｜{item['path']}", file=output)
    elif status in (STATUS_MATCH, STATUS_DRIFT, STATUS_UNREADABLE):
        if result.get("reason"):
            print(result["reason"], file=output)
    elif status == STATUS_DRY_RUN:
        print(result["text"], end="" if result["text"].endswith("\n") else "\n", file=output)
    summary = (
        "CORE-GEN {status} bytes={bytes} cap={cap} rules={rules} floor={floor} "
        "resident={resident} skipped={skipped} out={out}"
    )
    if status in (STATUS_MATCH, STATUS_DRIFT, STATUS_UNREADABLE):
        print(
            "CORE-GEN {status} bytes={bytes} rules={rules} out={out}".format(**result),
            file=output,
        )
    else:
        print(summary.format(**result), file=output)


_FIXTURES = {
    # 全部是合成規則（英文與另一種語言各有），不是任何人的真規則。
    "floor-2.md": ("floor", "base", 20, "Second synthetic floor sentence.", None, None),
    "floor-1.md": ("floor", "base", 10, "第一句合成底線規則。", None, None),
    "floor-3.md": ("floor", "base", 30, "Third synthetic floor sentence.", None, None),
    "resident-b2.md": ("resident", "beta", 220, "Beta section, second synthetic sentence.", None, None),
    "resident-a1.md": ("resident", "alpha", 110, "甲節第一句合成常駐規則。", None, None),
    "resident-b1.md": ("resident", "beta", 210, "乙節第一句合成常駐規則。", None, None),
    "resident-a2.md": ("resident", "alpha", 120, "Alpha section, second synthetic sentence.", None, None),
    "resident-a3.md": ("resident", "alpha", 130, "Alpha section, third synthetic sentence.", None, None),
    "resident-c1.md": ("resident", "gamma", 310, "Gamma section, only synthetic sentence.", None, None),
    "situational-1.md": ("situational", "alpha", 5, "Situational sentence that must not be generated.", None, None),
    "superseded-1.md": ("resident", "alpha", 1, "Superseded sentence that must not be generated.",
                        memspec.SUPERSEDED_DECISION_STATUS, "resident-a1.md"),
}


def _card_text(stem, layer, section, order, text, status, superseded_by,
               approved_by="synthetic-pair", approved_at="2026-09-09"):
    lines = [
        "---",
        f"name: {stem}",
        f"description: 2026-09-09 synthetic rule card {stem}",
        f"{memspec.RULE_LAYER_FIELD}: {layer}",
        f"{memspec.RULE_SECTION_FIELD}: {section}",
        f"{memspec.RULE_ORDER_FIELD}: {order}",
        f"{memspec.RULE_TEXT_FIELD}: {text}",
        f"{memspec.DECIDED_BY_FIELD}: {memspec.THREE_WAY_DECIDER}",
    ]
    if approved_by:
        lines.append(f"{memspec.RULE_APPROVED_BY_FIELD}: {approved_by}")
    if approved_at:
        lines.append(f"{memspec.RULE_APPROVED_AT_FIELD}: {approved_at}")
    if status:
        lines.append(f"{memspec.DECISION_STATUS_FIELD}: {status}")
    if superseded_by:
        lines.append(f"{memspec.SUPERSEDED_BY_FIELD}: {superseded_by}")
    lines += [
        f"{memspec.ALIASES_FIELD}:",
        f"  - {stem}",
        "  - 合成卡",
        "metadata:",
        f"  type: {memspec.CARD_TYPE_RULE}",
        "---",
        "body",
        "",
    ]
    return "\n".join(lines)


def _build_vault(root):
    root.mkdir(parents=True, exist_ok=True)
    for stem, spec in _FIXTURES.items():
        (root / stem).write_text(_card_text(stem[:-3], *spec), encoding="utf-8", newline="\n")
    # 一張非規則卡：生成器只吃規則卡，別的型別一個字都不能進核心塊。
    (root / "feedback-noise.md").write_text(
        "---\nname: feedback-noise\ndescription: 2026-09-09 not a rule card\n"
        "aliases:\n  - noise\nmetadata:\n  type: feedback\n---\nThis body must never be generated.\n",
        encoding="utf-8",
        newline="\n",
    )
    return root


_SELFTEST_ENV_NAMES = ("HOME", "USERPROFILE", memspec.EPITYPE_CONFIG_ENV)


def _selftest():
    checks = []
    saved_environ = {name: os.environ.get(name) for name in _SELFTEST_ENV_NAMES}
    try:
        with tempfile.TemporaryDirectory(prefix="epitype-coregen-") as temp_dir:
            root = Path(temp_dir).resolve()
            vault = _build_vault(root / "vault")
            out = root / "core_block.md"

            # CLI 那幾個案例走 `main()`，它拿不到 config 參數就讀真設定檔——這台機器
            # 的 `core_cap_bytes` 一旦設下去，自測會被真上限擋成 over-cap。家目錄一起
            # 指走：沒有 EPITYPE_CONFIG 時設定路徑是從家目錄推的（外層 finally 還原）。
            home = root / "home"
            home.mkdir()
            selftest_config = root / "selftest-config.json"
            selftest_config.write_text(
                json.dumps({memspec.CONFIG_VAULTS_FIELD: [os.fspath(vault)]}, ensure_ascii=False),
                encoding="utf-8",
            )
            os.environ["HOME"] = os.fspath(home)
            os.environ["USERPROFILE"] = os.fspath(home)
            os.environ[memspec.EPITYPE_CONFIG_ENV] = os.fspath(selftest_config)

            first = generate([vault], out, config={})
            text = out.read_text(encoding="utf-8")
            body = text.splitlines()
            checks.append((
                "生成 floor 編號段與 resident 分節，situational 與 superseded 都不出現",
                first["status"] == STATUS_WRITTEN
                and first["floor"] == 3
                and first["resident"] == 6
                and first["skipped"] == 1
                and "Situational sentence" not in text
                and "Superseded sentence" not in text
                and "This body must never be generated." not in text,
            ))
            checks.append((
                "floor 依 order 編號 1. 2. 3.；resident 節序由節內最小 order 決定",
                body[:2] == [
                    memspec.CORE_GEN_OUTPUT_TITLE,
                    memspec.CORE_GEN_OUTPUT_NOTE.format(floor=3, resident=6),
                ]
                and "1. 第一句合成底線規則。" in body
                and "2. Second synthetic floor sentence." in body
                and "3. Third synthetic floor sentence." in body
                and [line for line in body if line.startswith("### ")]
                == ["### alpha", "### beta", "### gamma"],
            ))
            checks.append((
                "節內依 order；每條 resident 規則一行，行首只加「- 」",
                text.index("- 甲節第一句合成常駐規則。")
                < text.index("- Alpha section, second synthetic sentence.")
                < text.index("- Alpha section, third synthetic sentence.")
                < text.index("- 乙節第一句合成常駐規則。")
                < text.index("- Beta section, second synthetic sentence."),
            ))
            checks.append((
                "逐位元組照抄：每張生成卡的 text 原樣出現在輸出裡（含非 ASCII）",
                all(
                    spec[3].encode("utf-8") in out.read_bytes()
                    for spec in _FIXTURES.values()
                    if spec[0] in memspec.RULE_GENERATED_LAYERS and not spec[4]
                ),
            ))

            pack = json.loads(pack_path(vault).read_text(encoding="utf-8"))
            carded = {item["path"]: item for item in pack["cards"]}
            checks.append((
                "核准包欄位齊全：每張卡的路徑、text 雜湊、層／節／序、核准者，加輸出檔雜湊與位元組",
                first["pack"] == os.fspath(pack_path(vault))
                and pack["out"] == os.fspath(out)
                and pack["out_bytes"] == len(out.read_bytes())
                and pack["out_sha256"] == _sha256(out.read_bytes())
                and pack["cap_bytes"] is None
                and set(carded) == {
                    stem for stem, spec in _FIXTURES.items()
                    if spec[0] in memspec.RULE_GENERATED_LAYERS and not spec[4]
                }
                and carded["resident-a1.md"]["approved_by"] == "synthetic-pair"
                and carded["resident-a1.md"]["section"] == "alpha"
                and carded["resident-a1.md"]["order"] == 110
                and carded["resident-a1.md"]["text_sha256"]
                == _sha256("甲節第一句合成常駐規則。".encode("utf-8"))
                and "generated_at" in pack,
            ))

            checked = check([vault], out)
            checks.append((
                "剛生成的檔 --check 是一致",
                checked["status"] == STATUS_MATCH and checked["reason"] is None,
            ))
            out.write_text(text + "手改的一行\n", encoding="utf-8", newline="\n")
            drifted_result = check([vault], out)
            checks.append((
                "手改一行就測得出漂移；drifted() 給同一個答案",
                drifted_result["status"] == STATUS_DRIFT
                and os.fspath(out) in drifted_result["reason"]
                and drifted([vault], out) is True,
            ))
            checks.append((
                "沒有規則卡的庫不判漂移（空組裝比不出東西），讀不到的檔也不判",
                drifted([root / "vault-empty"], out) is None
                and drifted([vault], root / "nowhere.md") is None,
            ))
            regenerated = generate([vault], out, config={})
            checks.append((
                "重生成把手改的行蓋回去，內容沒變時回 unchanged",
                regenerated["status"] == STATUS_WRITTEN
                and out.read_text(encoding="utf-8") == text
                and generate([vault], out, config={})["status"] == STATUS_UNCHANGED,
            ))

            before = out.read_bytes()
            dry = generate([vault], out, dry_run=True, config={})
            checks.append((
                "--dry-run 只印不寫，也不動核准包",
                dry["status"] == STATUS_DRY_RUN
                and dry["text"] == text
                and out.read_bytes() == before,
            ))

            capped = generate([vault], out, cap_bytes=len(before) - 1, config={})
            checks.append((
                "超上限＝拒絕寫出、列最長的卡、既有檔一個位元組都沒動",
                capped["status"] == STATUS_OVER_CAP
                and capped["cap"] == len(before) - 1
                and 0 < len(capped["offenders"]) <= memspec.CORE_GEN_LONGEST_LISTED
                and capped["offenders"][0]["bytes"]
                >= capped["offenders"][-1]["bytes"]
                and out.read_bytes() == before,
            ))
            checks.append((
                "上限由設定的 core_cap_bytes 供給；沒設就不擋",
                generate([vault], out, config={memspec.CONFIG_CORE_CAP_BYTES_FIELD: 10},
                         dry_run=True)["status"] == STATUS_OVER_CAP
                and generate([vault], out, config={}, dry_run=True)["cap"] is None,
            ))

            bare = _build_vault(root / "bare")
            (bare / "floor-1.md").write_text(
                _card_text("floor-1", "floor", "base", 10, "Approval-less synthetic sentence.",
                           None, None, approved_by="", approved_at=""),
                encoding="utf-8",
                newline="\n",
            )
            bare_out = root / "bare_block.md"
            refused = generate([bare], bare_out, config={})
            checks.append((
                "生成層的卡缺核准者或核准日期＝拒絕生成並列出，不寫出任何檔案",
                refused["status"] == STATUS_UNAPPROVED
                and [item["path"] for item in refused["offenders"]] == ["floor-1.md"]
                and not bare_out.exists()
                and not pack_path(bare).exists(),
            ))

            output = io.StringIO()
            code = main([os.fspath(vault), "--out", os.fspath(root / "cli_block.md")], output=output)
            printed = output.getvalue()
            checks.append((
                "CLI 生成回 0 並印出摘要行",
                code == 0
                and "CORE-GEN written" in printed
                and "floor=3" in printed
                and (root / "cli_block.md").is_file(),
            ))
            output = io.StringIO()
            code = main(
                ["--check", os.fspath(vault), "--out", os.fspath(root / "cli_block.md")],
                output=output,
            )
            checks.append((
                "--check 一致回 0",
                code == 0 and "CORE-GEN match" in output.getvalue(),
            ))
            (root / "cli_block.md").write_text("drifted\n", encoding="utf-8", newline="\n")
            output = io.StringIO()
            code = main(
                ["--check", os.fspath(vault), "--out", os.fspath(root / "cli_block.md")],
                output=output,
            )
            checks.append((
                "--check 漂移回非零，且不把檔改回去",
                code == EXIT_REFUSED
                and "CORE-GEN drift" in output.getvalue()
                and (root / "cli_block.md").read_text(encoding="utf-8") == "drifted\n",
            ))
            output = io.StringIO()
            code = main(
                [os.fspath(vault), "--out", os.fspath(root / "capped.md"), "--cap-bytes", "10"],
                output=output,
            )
            checks.append((
                "自測全程指著暫存的設定與家目錄：走 main() 的案例讀不到這台機器的真設定與真上限",
                memspec.config_path() == selftest_config
                and Path.home() == home
                and generate([vault], root / "isolated.md", dry_run=True)["cap"] is None,
            ))
            checks.append((
                "CLI 超上限回非零且沒有寫出檔案",
                code == EXIT_REFUSED
                and not (root / "capped.md").exists()
                and "CORE-GEN over-cap" in output.getvalue(),
            ))
    except Exception as exc:
        print(f"SELFTEST ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        # 指向是行程層的，失敗路徑也要還原：留著的話，同一個行程裡後面跑的東西會
        # 對著一個已經被刪掉的暫存目錄找家。
        for name, value in saved_environ.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    passed = sum(bool(ok) for _, ok in checks)
    total = 18
    status = "PASS" if passed == total and len(checks) == total else "FAIL"
    print(f"SELFTEST {status} {passed}/{total}")
    if status != "PASS":
        for name, ok in checks:
            if not ok:
                print(f"FAILED: {name}", file=sys.stderr)
    return 0 if status == "PASS" else 1


def main(argv=None, output=sys.stdout):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--selftest"]:
        return _selftest()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vaults", nargs="+", type=Path)
    parser.add_argument("--out", required=True, type=Path, help="file to assemble the core block into")
    parser.add_argument("--cap-bytes", type=int, default=None,
                        help="refuse to write past this size (default: config core_cap_bytes; unset means no cap)")
    parser.add_argument("--dry-run", action="store_true", help="print the assembly, write nothing")
    parser.add_argument("--check", action="store_true",
                        help="compare the assembly with --out byte for byte; exit non-zero when they differ")
    parser.add_argument("--json", action="store_true")
    parsed = parser.parse_args(arguments)

    try:
        if parsed.check:
            result = check(parsed.vaults, parsed.out)
        else:
            result = generate(
                parsed.vaults, parsed.out, cap_bytes=parsed.cap_bytes, dry_run=parsed.dry_run
            )
    except Exception as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if parsed.json:
        print(json.dumps(
            {key: value for key, value in result.items() if key != "text"},
            ensure_ascii=False, indent=1,
        ), file=output)
    else:
        _print(result, output)
    if result["status"] in (STATUS_OVER_CAP, STATUS_UNAPPROVED, STATUS_DRIFT, STATUS_UNREADABLE):
        return EXIT_REFUSED
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
