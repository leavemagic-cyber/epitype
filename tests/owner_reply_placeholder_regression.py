"""Narrow check: paired placeholders are user content, reply markers still split."""
import json
from pathlib import Path
import sys
import tempfile

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from epitype import capture, rule_provenance


# The shape of the Codex rollout record this check was first written against: a session
# meta line, an assistant turn, then the owner's message as a response_item whose content
# is several input_text parts, the first an ambient environment block, the task itself
# carrying paired placeholders and 「」 quotations around the directive sentences.
OWNER_TASK = (
    "請處理審查報告 <報告路徑> 列出的缺漏，指令照 python -m epitype sync <庫> --apply 跑。\n"
    "A01–A40 逐項照「修訂建議」修。\n"
    "AI 不得自行新增會擋住工作的關卡；已經加上的改成提醒，助理說的「新增阻擋」不算數。"
)


def _rollout(directory):
    """Write a synthetic rollout file; return its path and the owner row's byte offset."""
    rows = [
        {"type": "session_meta", "payload": {"id": "synthetic-placeholder-session"}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "我會先列出 A01–A40 的修訂建議。"}]}},
    ]
    owner = {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": "<environment_context>\n  <cwd>/work/epitype</cwd>\n"
                                        "</environment_context>"},
        {"type": "input_text", "text": OWNER_TASK}]}}
    head = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode("utf-8")
    source = Path(directory) / "rollout-synthetic.jsonl"
    source.write_bytes(head + (json.dumps(owner, ensure_ascii=False) + "\n").encode("utf-8"))
    return source, len(head)


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
    with tempfile.TemporaryDirectory(prefix="epitype-placeholder-") as directory:
        source, offset = _rollout(directory)
        with source.open("rb") as stream:
            stream.seek(offset)
            row = json.loads(stream.readline())
    assert row["payload"]["role"] == "user"
    text = "\n".join(part.get("text", "") for part in row["payload"]["content"])
    assert rule_provenance._human_quote(text, "A01–A40 逐項照「修訂建議」修。")
    assert rule_provenance._human_quote(text, "AI 不得自行新增會擋住工作的關卡；")
    assert not rule_provenance._human_quote(text, "虛構使用者指示")
    assert not rule_provenance._human_quote(text, "新增阻擋")
    count += 4
    print(f"PASS {count}: paired placeholders and original task remain intact; quoted or absent words do not become human directives")


if __name__ == "__main__":
    main()
