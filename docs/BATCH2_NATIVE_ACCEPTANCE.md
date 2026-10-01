# 第二批原生送達與壓縮驗證

2026-10-01 接續修復，替代初次交接中 C／D 僅有介面測試的未驗證結論。私有原始 transcript、稽核 ID、備份與來源映射保存在本機 repo 外；公開文件只記方法和數字。

## 工具前提醒

Claude Code 2.1.280：Sonnet／low 的單純宿主接線測試，六個獨立 Bash 呼叫為 rg、git、python 各兩輪。前三次各收到既有規則原文，後三次不重送；原生結果 success，7 turns、46,719ms。Codex 本場原生 developer 訊息確認同三張卡各一次。完整入口耗時及範圍見 [工具前補送](BATCH2_TOOL_ADVICE.md)。測試證明送達，不保證任意模型總會遵守。

## Claude 真正自動壓縮

沿用同一測試場，以官方 `--settings` 只對該次 resume 覆寫 `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=1`。沒有修改全域 settings；全域值仍為 30。這是短小可控的原生壓縮測試，不將 1 作產品預設。

|原生 auto boundary|壓縮前 token|壓縮後 token|送回規則數|規則 UTF-8 bytes|
|---|---:|---:|---:|---:|
|第一次|40,895|3,267|6|1,729|
|第二次|36,753|2,901|6|1,711|

兩次 Epitype SessionStart:compact 均 exit 0；新的規則描述在第一個 Bash 前送達，模型回覆逐張列出六卡，單次執行 python --version 成功。這六卡取本場已用指標的預算內最近項，不聲稱恰好重送工具前測試的三卡。既有 1,800-byte 上限、100 個指標與有效卡檢查保留。

本場 Codex 自然壓縮後的原生 developer 訊息也已實際送回規則描述與壓縮前地圖／交接指標；不是僅測 adapter stdout。

Claude print 結果的 total_cost_usd 是客戶端估計；resume 回報整段對話累計。本次工具前測試 0.2163244、壓縮測試累計 0.4627983，不能當實際帳單或新增花費等於後者。壓縮測試退出時另有既有 memory_lint.py SessionEnd hook cancelled；Epitype 壓縮 hook 成功，這個外部 lint 的收尾未宣稱通過。

官方依據：[環境變數](https://code.claude.com/docs/en/env-vars)、[設定優先順序及 --settings](https://code.claude.com/docs/en/settings)、[非互動模式與累計費用估計](https://code.claude.com/docs/en/headless)、[Claude hooks](https://code.claude.com/docs/en/hooks)、[Codex hooks](https://learn.chatgpt.com/docs/hooks)。
