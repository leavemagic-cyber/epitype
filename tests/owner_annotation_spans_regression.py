"""Mixed quoted-list annotations retain each human span without creating quotes."""
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from epitype import capture, compact_map, rule_provenance
import json


def main():
    count = 0
    cited = ("休眠項由你決定。", "影片項可以清理。", "專案項等你回報。")
    replies = ("請先說明後遺症？", "那項你不用管", "甲已處理，乙你可以查")
    text = cited[0] + "《" + replies[0] + "\n\n" + cited[1] + "〈" + replies[1] + "\n\n" + cited[2] + "《" + replies[2]
    assert capture.owner_replies(text) == list(replies)
    for quote in replies:
        assert rule_provenance._human_quote(text, quote)
        count += 1
    for quote in cited:
        assert not rule_provenance._human_quote(text, quote)
        count += 1
    assert not rule_provenance._human_quote(text, replies[0] + "\n\n" + replies[1])
    count += 1
    same_block = "- quote one《owner one\n- quote two〈owner two"
    assert capture.owner_replies(same_block) == ["owner one", "owner two"]
    assert not rule_provenance._human_quote(same_block, "quote two")
    count += 1
    for marker in ("《", "〈", "<-", "<=", "<"):
        sample = "quoted-prefix" + marker + "人類回覆"
        assert capture.owner_reply_parts(sample) == ("quoted-prefix", "人類回覆")
        assert not rule_provenance._human_quote(sample, "quoted-prefix")
        count += 1
    for sample in ("指令 <庫> 保留", "文件《篇名》保留", "文件〈篇名〉保留"):
        assert capture.owner_reply_parts(sample) == ("", sample)
        count += 1
    assert not rule_provenance._human_quote("prefix《第一批註\n\nquoted《「助理限制」只是引用", "助理限制")
    count += 1
    raw = json.dumps({"type": "user", "message": {"content": text}}, ensure_ascii=False).encode()
    users, _ = compact_map._extract_candidates([(1, raw)])
    assert all(reply in users[0][2] for reply in replies)
    assert "cited_text=" in users[0][2]
    count += 1
    assert not capture.auto_admitted("資料夾 <庫> 好，以後都照辦")[0]
    count += 1
    print(f"PASS {count}: each annotation is human; cited prefixes, joined quotations and titles are excluded")


if __name__ == "__main__":
    main()
