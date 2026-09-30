# 工具使用前補送

判斷型卡保留 `unenforceable`，另加 `advise_tool`（工具名稱）、可選 `advise_all_of`（參數中全數須出現的字面片段）、`advise_text`（規則原文摘錄，最多 240 字）。只提醒，不改成禁止。

```yaml
unenforceable: 需要判斷資料是否足夠
advise_tool: Bash
advise_all_of: ["rg"]
advise_text: 如果 owner 說資料存在，先查能讀的來源，再說資料不足。
```

沿用守衛的檔頭快取，不另讀整庫正文。至多兩條，依實際卡路徑同場去重；尚未送到的第三條保留下一次機會。已禁止的動作不會消耗提醒資格。

驗證：`tests/tool_advice_regression.py` 三題通過，Bash 與 exec_command 各三張文字卡，整個測試 0.230 秒，各次 hook 均小於 2 秒。同場去重、兩條上限與未命中不送均有斷言。這是 hook 介面驗證，真實宿主模型採用仍未驗證。

宿主依據：[Claude hooks](https://code.claude.com/docs/en/hooks)、[Codex hooks](https://learn.chatgpt.com/docs/hooks) 的 PreToolUse `hookSpecificOutput.additionalContext`。規則會進模型上下文，宿主不保證顯示成聊天訊息。
