"""Fixture-only A37 source labels, supersession and budget omissions."""
import json
from pathlib import Path
import sys
import tempfile
import uuid

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "adapters" / "claude"))
from epitype import memsearch, memspec
import _hook_common as common
import recall_hook


def main():
    count = 0
    with tempfile.TemporaryDirectory(prefix="epitype-a37-") as directory:
        root = Path(directory)
        vault = root / "vault"
        vault.mkdir()
        (vault / "old.md").write_text("---\nname: old\ndescription: advisoryneedle retired\ndecision_key: advisory-rule\nstatus: superseded\nsuperseded_by: current.md\n---\nadvisoryneedle retired body\n", encoding="utf-8")
        (vault / "current.md").write_text("---\nname: current\ndescription: advisoryneedle active pointer\ndecision_key: advisory-rule\nstatus: active\n---\nadvisoryneedle current body\n", encoding="utf-8")
        memsearch.build_index(vault)
        config = root / "config.json"
        common.write_config(config, [vault])
        result = common.run_synthetic(REPO / "adapters" / "claude" / "recall_hook.py",
                                     {"prompt": "advisoryneedle", "session_id": "a37-" + uuid.uuid4().hex}, config)
        assert result.returncode == 0, result.stderr
        value = json.loads(result.stdout)
        context = value["hookSpecificOutput"]["additionalContext"]
        assert "查找線索（來源未核；非授權或新增禁令）" in context
        assert "⚖ 裁定" not in context and "retired body" not in context
        assert "current.md" in context and "permissionDecision" not in result.stdout
        count += 4
        notice = recall_hook._source_omission(1000)
        assert "預算不足" in notice["hookSpecificOutput"]["additionalContext"]
        assert "未交付裁定" in notice["hookSpecificOutput"]["additionalContext"]
        count += 2
    print(f"PASS {count}: unknown source stays a pointer, superseded rule is absent, budget omission is visible and non-blocking")


if __name__ == "__main__":
    main()
