"""Narrow check: paired placeholders are user content, reply markers still split."""
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from epitype import capture, rule_provenance


def main():
    count = 0
    for text in ("python -m epitype sync <庫> --apply --actor codex",
                 "任務 <案件名稱> 應保留原文", "依 <使用者 指令> 繼續"):
        assert capture.owner_reply_parts(text) == ("", text)
        assert rule_provenance._human_quote(text, text)
        count += 1
    for marker in ("<-", "<=", "《", "<"):
        text = "assistant quoted text " + marker + "使用者請继续修正"
        assert capture.owner_reply_parts(text) == ("assistant quoted text", "使用者請继续修正")
        assert not rule_provenance._human_quote(text, "assistant quoted text")
        assert rule_provenance._human_quote(text, "使用者請继续修正")
        count += 1
    assert not rule_provenance._human_quote("助理說「新增阻擋」，我只是引用。", "新增阻擋")
    count += 1
    source = Path(r"C:\Users\User\.codex\sessions\2026\10\03\rollout-2026-10-03T21-54-14-01a1020b-4219-73c3-9c69-5a95090a050f.jsonl")
    with source.open("rb") as stream:
        stream.seek(169473)
        row = json.loads(stream.readline())
    assert row["payload"]["role"] == "user"
    text = "\n".join(part.get("text", "") for part in row["payload"]["content"])
    assert rule_provenance._human_quote(text, "A01–A40 逐項照「修訂建議」修。")
    assert rule_provenance._human_quote(text, "AI 不得自行新增會擋住工作的關卡；")
    assert not rule_provenance._human_quote(text, "虛構使用者指示")
    count += 3
    print(f"PASS {count}: paired placeholders and original task remain intact; quoted or absent words do not become human directives")


if __name__ == "__main__":
    main()
