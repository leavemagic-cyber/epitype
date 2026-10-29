"""Duplicate candidates and report-only CLI/nightly wiring on isolated fixtures."""
import sys
sys.dont_write_bytecode = True
for stream in (sys.stdout, sys.stderr):
    getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace")

from contextlib import redirect_stdout
from datetime import date
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from epitype import cli, dream, dupes, memspec, views


def card(vault, name, body, extra=""):
    path = vault / (name + ".md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: synthetic\n{extra}---\n{body}\n", encoding="utf-8")
    return path


def main():
    checks = []
    with tempfile.TemporaryDirectory(prefix="epitype-dupes-test-") as temp_dir:
        root = Path(temp_dir).resolve()
        a, b = root / "a", root / "b"
        a.mkdir()
        b.mkdir()
        text = "收到完成通知後先讀實際產物，不能單憑結束碼判斷成功。" * 4
        old = card(a, "result-old", text, "current_decision_at: 2026-10-01\n")
        new = card(b, "result-new", text + "確認內容與預期一致。", "current_decision_at: 2026-10-29\n")
        card(a, "obsolete", text, "status: superseded\n")
        card(a, "history", text, "scope: historical\n")
        for directory in ("grants", "rulings", "corrections", "_drafts", ".hidden"):
            card(a, directory + "/event", text)
        (a / "MEMORY.md").write_text(text, encoding="utf-8")
        report = dupes.detect([a, b, a], today=date(2026, 10, 29))
        checks.append(("Chinese variants cluster; excludes retired/historical/capture/private/index files", report["cards_scanned"] == 2 and len(report["clusters"]) == 1 and not report["errors"]))
        checks.append(("newest wins absent machine fields", report["clusters"][0]["suggested_keep"] == str(new)))
        old.write_text(old.read_text(encoding="utf-8").replace("description: synthetic", "description: synthetic\nforbidden:\n  - exit zero\nverify: verify-result\nhost_native_custom: true"), encoding="utf-8")
        report = dupes.detect([a, b], today=date(2026, 10, 29))
        cluster = report["clusters"][0]
        checks.append(("machine-check carrier preferred and host_native wildcard retained", cluster["suggested_keep"] == str(old) and {"forbidden", "verify", "host_native_custom"} <= set(cluster["cards"][0]["machine_fields"])))
        checks.append(("actual Jaccard reported separately", 0 < cluster["pairs"][0]["similarity"] < 1))
        quote = dupes.newest_owner_quote({"owner_quote": "2026-10-02 「早期原話」"}, "## Owner 2026-10-20\n> 「最新原話」\n## 2026-10-28 維護\nAI 更新完成。")
        checks.append(("owner quote dates exclude newer maintenance dates", quote["date"] == "2026-10-20" and "最新原話" in quote["text"]))
        checks.append(("undated quotes explicitly undated", dupes.newest_owner_quote({"owner_quote": "「未標日期」"}, "")["date"] is None))
        before = {p: p.read_bytes() for vault in (a, b) for p in vault.rglob("*.md")}
        (b / memspec.WORK_LEDGER_FILENAME).write_text("ledger\n", encoding="utf-8")
        with redirect_stdout(io.StringIO()) as output:
            code = cli.main(["dupes", str(a), str(b), "--json"])
        printed = json.loads(output.getvalue())
        written = b / "_drafts" / "duplicates" / (printed["date"].replace("-", "") + ".md")
        checks.append(("CLI ledger governance and Markdown/JSON output", code == 0 and written.is_file() and json.loads(written.with_suffix(".json").read_text(encoding="utf-8")) == printed))
        checks.append(("all source card bytes preserved", all(p.read_bytes() == content for p, content in before.items())))
        checks.append(("report contains owner policy and fields", "owner" in written.read_text(encoding="utf-8") and "forbidden" in written.read_text(encoding="utf-8")))
        checks.append(("generated reports do not inflate pending-draft or aging counts", dream._drafts_of(b) == ([], 0) and dream._draft_aging_of(b, date(2026, 10, 29)) == []))
        c = root / "signals"
        c.mkdir()
        card(c, "key-left", "甲乙丙丁", "decision_key: one\n")
        card(c, "key-right", "戊己庚辛", "decision_key: one\n")
        card(c, "alias-left", text, "aliases: [完成查核]\n")
        card(c, "alias-right", text + "確認結果。", "metadata:\n  aliases:\n    - 完成查核\n")
        card(c, "link-left", "午未申酉規則需要實際驗證讀取路徑 [[link-right]]")
        card(c, "link-right", "午未申酉規則需要實際驗證讀取路徑 [[link-left]]")
        card(c, "deployment-verification-one", "部署前後需要分別確認實際效果")
        card(c, "deployment-verification-two", "部署前後需要分別確認實際效果。")
        all_signals = {s for group in dupes.detect([c])["clusters"] for p in group["pairs"] for s in p["signals"]}
        checks.append(("key, whole/nested aliases, mutual links and near-name signals", {"decision_key", "shared_alias", "mutual_wikilink", "near_name"} <= all_signals))
        checks.append(("aliases never split into individual characters", "shared_alias" not in {s for group in dupes.detect([a, b])["clusters"] for p in group["pairs"] for s in p["signals"]}))
        chain = root / "chain"
        chain.mkdir()
        card(chain, "first", "甲乙丙丁", "decision_key: same\n")
        card(chain, "second", "戊己庚辛", "decision_key: same\naliases: [bridge]\n")
        card(chain, "third", "戊己庚辛", "aliases: [bridge]\n")
        checks.append(("connected components group pair chains", len(dupes.detect([chain])["clusters"][0]["cards"]) == 3))
        judgments = root / "judgments"
        chain_report = dupes.detect([chain], governance=judgments)
        report_path = root / "review.json"
        verdict_path = root / "verdicts.md"
        report_path.write_text(json.dumps(chain_report), encoding="utf-8")
        verdict_path.write_text("OVERLAP 1 | separate uses\n", encoding="utf-8")
        imported = dupes.import_verdicts(report_path, verdict_path, judgments)
        filtered = dupes.detect([chain], governance=judgments)
        checks.append(("unchanged judged edges removed before clustering", imported["pairs"] == 2
                       and filtered["suppressed_pairs"] == 2 and filtered["clusters"] == []))
        first = chain / "first.md"
        original = first.read_text(encoding="utf-8")
        first.write_text(original + "new owner wording\n", encoding="utf-8")
        changed = dupes.detect([chain], governance=judgments)
        checks.append(("changed body restores only its edges", changed["suppressed_pairs"] == 1
                       and len(changed["clusters"]) == 1 and len(changed["clusters"][0]["cards"]) == 2))
        # Importing a saved report must not bless changes made after the scan.
        dupes.import_verdicts(report_path, verdict_path, judgments)
        checks.append(("report-time fingerprints survive delayed import", len(dupes.detect([chain], governance=judgments)["clusters"]) == 1))
        first.write_text(original, encoding="utf-8")
        first.write_text(original.replace("description: synthetic", "description: edited metadata"), encoding="utf-8")
        checks.append(("metadata-only edit preserves body judgment", not dupes.detect([chain], governance=judgments)["clusters"]))
        first.write_text(original, encoding="utf-8")
        verdict_path.write_text("DUPLICATE 1 | still needs settlement\n", encoding="utf-8")
        dupes.import_verdicts(report_path, verdict_path, judgments)
        checks.append(("duplicate verdict does not hide unsettled candidates", dupes.detect([chain], governance=judgments)["suppressed_pairs"] == 0))
        for item in chain_report["clusters"][0]["cards"]:
            item.pop("body_fingerprint")
        report_path.write_text(json.dumps(chain_report), encoding="utf-8")
        verdict_path.write_text("OTHER-VAULT 1 | UNRELATED | template only\n", encoding="utf-8")
        legacy = dupes.import_verdicts(report_path, verdict_path, judgments)
        checks.append(("legacy reports explicitly capture current fingerprints", legacy["current_fingerprints"] == 3
                       and not dupes.detect([chain], governance=judgments)["clusters"]))
        parsed = dupes.parse_verdicts("SKIPPED-BINDING 1 | verify, owner_quote | OVERLAP：different scopes\n", 1)
        checks.append(("protected binding verdict syntax supported", parsed == {1: ("overlap", "different scopes")}))
        saved_judgments = dupes.judged_path(judgments).read_bytes()
        for invalid in ("", "OVERLAP 1 | why\nOVERLAP 1 | repeated", "UNRELATED 2 | wrong index", "unknown verdict"):
            verdict_path.write_text(invalid, encoding="utf-8")
            try:
                dupes.import_verdicts(report_path, verdict_path, judgments)
            except ValueError:
                checks.append(("invalid import preserves judgments", dupes.judged_path(judgments).read_bytes() == saved_judgments))
            else:
                checks.append(("invalid import rejected", False))
        with redirect_stdout(io.StringIO()) as isolated_output:
            isolated_code = cli.main(["dupes", str(chain), "--governance", str(judgments), "--json"])
        checks.append(("governance override reads and writes only the copied record/report", isolated_code == 0
                       and json.loads(isolated_output.getvalue())["suppressed_pairs"] == 2
                       and not (chain / "_drafts").exists()))
        empty = root / "empty"
        empty.mkdir()
        card(empty, "one", "")
        card(empty, "two", "")
        checks.append(("empty bodies not identical evidence", not dupes.detect([empty])["clusters"]))
        card(empty, "weak-a", "甲乙丙丁", "aliases: [generic]\n")
        card(empty, "weak-b", "戊己庚辛", "aliases: [generic]\n")
        checks.append(("a shared alias alone cannot connect unrelated rule bodies", not dupes.detect([empty])["clusters"]))
        broken = card(empty, "broken", "body")
        broken.write_bytes(b"\xff")
        checks.append(("bad UTF-8 exposed as incomplete coverage", bool(dupes.detect([empty])["errors"])))
        try:
            dupes.detect([a], threshold=0)
        except ValueError:
            checks.append(("invalid threshold rejected", True))
        else:
            checks.append(("invalid threshold rejected", False))
        checks.append(("compact filename dates recognised", dupes._dates("rule-20261029") == ["2026-10-29"]))
        checks.append(("empty machine fields do not masquerade as checks", dupes.machine_fields(["forbidden: []", "verify:"], {"forbidden": "[]", "verify": ""}) == []))
        home = root / "home"
        config = home / ".epitype" / "config.json"
        config.parent.mkdir(parents=True)
        (home / ".claude").mkdir()
        config.write_text(json.dumps({"vaults": [str(a), str(b)]}), encoding="utf-8")
        env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), EPITYPE_CONFIG=str(config), CODEX_HOME=str(home / ".codex"), PYTHONIOENCODING="utf-8")
        result = subprocess.run([sys.executable, "epitype/dream.py", "--scheduled", "--today", "2026-10-29"], cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, encoding="utf-8")
        nightly = b / "_drafts" / "duplicates" / "20261029.json"
        pack = b / memspec.DREAM_DIRECTORY / memspec.DREAM_PACK_JSON_FILENAME
        checks.append(("real isolated scheduled dream writes duplicate report and pack summary", result.returncode == 0 and nightly.is_file() and pack.is_file() and json.loads(pack.read_text(encoding="utf-8"))["duplicates"]["path"] == str(nightly.with_suffix(".md"))))
        calls = []
        generate_original, detect_original = views.generate, dupes.detect

        def generate(vault):
            calls.append("views")
            return generate_original(vault)

        def detect(*args, **kwargs):
            calls.append("dupes")
            return detect_original(*args, **kwargs)

        saved = {name: os.environ.get(name) for name in ("HOME", "USERPROFILE", "EPITYPE_CONFIG", "CODEX_HOME")}
        os.environ.update({name: env[name] for name in saved})
        try:
            with redirect_stdout(io.StringIO()) as configured_output:
                configured_code = cli.main(["dupes", "--json"])
            checks.append(("omitting vaults uses the shared configured vault discovery", configured_code == 0 and json.loads(configured_output.getvalue())["cards_scanned"] == 2))
            with patch.object(views, "generate", side_effect=generate), patch.object(dupes, "detect", side_effect=detect):
                dream.main([str(a), str(b), "--out", str(root / "pack.md"), "--today", "2026-10-29"], output=io.StringIO())
            checks.append(("nightly scan follows all view regeneration", calls[:3] == ["views", "views", "dupes"]))
            with patch.object(dupes, "write_report", side_effect=AssertionError("dry-run wrote report")):
                dry_code = dream.main([str(a), str(b), "--dry-run", "--json"], output=io.StringIO())
            checks.append(("dream dry-run does not write duplicate reports", dry_code == 0))
            with patch.object(dupes, "detect", side_effect=OSError("synthetic failure")):
                dream.main([str(a), str(b), "--out", str(root / "failed-pack.json"), "--json"], output=io.StringIO())
            checks.append(("nightly failure visible in pack", "synthetic failure" in json.loads((root / "failed-pack.json").read_text(encoding="utf-8"))["duplicates"]["error"]))
            failed_state = json.loads((root / memspec.DREAM_STATE_FILENAME).read_text(encoding="utf-8"))
            checks.append(("duplicate report failure also marks nightly state incomplete", not failed_state[memspec.DREAM_STATE_COMPLETE_FIELD] and "duplicates" in failed_state[memspec.DREAM_STATE_ERRORS_FIELD]))
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        english = subprocess.run([sys.executable, "-m", "epitype", "dupes", str(empty)], cwd=Path(__file__).resolve().parents[1], env=dict(env, EPITYPE_LANG="en"), capture_output=True, text=True, encoding="utf-8")
        checks.append(("English output and incomplete-scan exit status", "Cross-vault near-duplicate cards" in english.stdout and english.returncode == 1))
    passed = sum(ok for _, ok in checks)
    for name, ok in checks:
        if not ok:
            print("FAILED: " + name, file=sys.stderr)
    print(f"SELFTEST {'PASS' if passed == len(checks) else 'FAIL'} {passed}/{len(checks)}")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
