# 工具使用前補送

判斷型卡保留 `unenforceable`，另加 `advise_tool`（工具名稱）、可選 `advise_all_of`（參數中全數須出現的字面片段）、`advise_text`（規則原文摘錄，最多 240 字）。只提醒，不改成禁止。

```yaml
unenforceable: 需要判斷資料是否足夠
advise_tool: Bash
advise_all_of: ["rg"]
advise_text: 如果 owner 說資料存在，先查能讀的來源，再說資料不足。
```

沿用守衛的檔頭快取，不另讀整庫正文。至多兩條，依實際卡路徑同場去重；尚未送到的第三條保留下一次機會。已禁止的動作不會消耗提醒資格。

最初介面驗證：三題通過，Bash 與 exec_command 各三張文字卡，整個測試 0.230 秒。後續加入輸出未送出時釋放去重資格，共四題通過。實際宿主送達證據見下方續修紀錄。

宿主依據：[Claude hooks](https://code.claude.com/docs/en/hooks)、[Codex hooks](https://learn.chatgpt.com/docs/hooks) 的 PreToolUse `hookSpecificOutput.additionalContext`。規則會進模型上下文，宿主不保證顯示成聊天訊息。

整庫副本實測：三張既有純文字卡（sources-outrank-recall、reuse-before-building、token-economy）先走正式 SessionStart 的暖快取函式，再用兩宿主工具格式各呼叫三次：0.2649／0.1646／0.1987 秒、0.2129／0.1632／0.1547 秒；六次均有對應規則上下文，同場第二次均不重送。冷快取只暖一個普通工具呼叫不足以遍歷 591 張卡；不能省略 SessionStart 暖快取而宣稱首次全面送達。

2026-10-01 續修：Claude Code 2.1.280 的真實非互動對話依序執行 rg、git、python，再重複三次；前三次各收到對應原規則，後三次沒有重送，原生結果 success。Codex 同場原生 developer 訊息也確認三張卡各送一次。這證明送達及去重，不能推導成模型永遠遵守。

最新完整 Python 子行程入口測量（591 張卡副本、私有設定與隔離 home、先暖快取、帶最近原生 transcript 查補）：Claude 格式 1.3695／0.7385／0.6913 秒；Codex 格式 0.8351／0.7413／0.7651 秒。六次均送對卡且重複不重送。測量包含 Python 啟動、既有 Pre 入口及回條查補；不是全機負載下最壞時間保證。早期未隔離 home 的量測另發現讀到兩庫並超過兩秒，profiling 定位到普通卡檔頭重讀兩次；已改一次，同時保留全部宣告及原刷新範圍，99 題守衛測試通過。
