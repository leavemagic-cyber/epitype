# 外部席執行

跨 AI 工作已獲授權且要用 CLI 才讀。`seats.py` 在本 skill 根目錄，一次指定一個或多個唯讀外部席；只跑當次名單。模型／effort 依難度、風險、整合責任和當前能力選，明確傳入，不寫死或為省 token 降級。Grok CLI → Claude App（窄 CLI 備援）→ Codex 只是合適時的偏好。

Codex 從 owner 的 `~/.codex/config.toml` 只讀模型／effort 必要欄位，不顯示憑證。**Codex 付費點數停用，須 owner 明確恢復**；未授權費用不得啟動。大批次或預估 Anthropic API 超過 US$5，先告知一次預估。

先用寫檔工具建 UTF-8 題目，不在 shell 行內拼貼 Markdown、反引號或 `$`。以下為參數示意，按實際 shell 引用路徑：

```text
python -B <seats.py> run <prompt.md> <紀錄夾> --tag R1 --cwd <目錄> --input <相關檔案> --seat codex:<model>:<effort>
python -B <seats.py> bundle <template.md> <紀錄夾> <out.md> --set TOPIC=<題目> --set MODE=<模式> --set WORKTREE=<目錄> --set REV=<版本>
```

Claude 的 shim 擋住時用 `cmd /c claude`；非互動審查優先 `claude -p`，限定題目檔與讀取範圍。送最少輸入前，先輪詢或讀取 stdin 狀態。

需要才加 `--seat grok:...`／`--seat agy:...`。其他席不寫正式 repo；協作作者需另一條獲授權的隔離實作路徑。不送憑證或無關對話，記憶只給相關有效要求及必要指標。

長任務用完成通知並保存識別與輸出位置，按需讀 log。runner 以題目、模型／effort、參數、來源及自身版本核對 receipt 和答案雜湊，符合才 `KEEP`。`--input` 可重複指定本題資料；只讀明列檔案內容，不自動掃整份歷史。失敗只補缺席，不重做仍有效結果。

`WRITE-DETECTED` 代表偵測來源狀態改變，該輪 receipts 失效，答案留存；查差異與歸屬，不一律還原。執行成功不等於內容正確，lead 仍需查證。用量、缺席、狀態與返回值詳 [執行補充](runtime-notes.md)，需要時才讀。
