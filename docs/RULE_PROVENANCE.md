# 現行內容與原話備查

Epitype 的目標是讓 AI 在正確時機取得正確、仍有效的記憶，照要求把事做對，壓縮後能接續，減少重犯與浪費。它不是原話考證系統。

這項原則適用整個 Epitype、所有宿主，以及現在和以後新增或修改的規則、記憶與程序。原話是備查資料，不是日常讀取、維護、生成或啟用的必要門檻。遇到具體歧義、衝突、歸屬爭議或明確要求查證時，再查必要片段；不因原件缺失、封存或讀取預算不足反覆掃描歷史。

## 日常流程

先確認目前要記的意思、適用專案與條件、例外、期限、作者及取代關係，並核對相關現行正本。AI 整理標示 AI 作者；舊狀態不能變成現在的啟動指示，使用者新指示不能被舊 AI 程序阻擋。正確規則被違反時，修真正失敗的步驟，不再追加同義教訓。

`rule_pipeline` 共用下列檢查：

- 目前規則文字、作者及完整適用範圍。
- 實際會使用的正文、執行欄位與已審內容是否一致。
- 有效期、雙向取代關係、退場狀態及目前事件是否適用。
- AI 提醒不自動取得阻擋權；內容檢查不授予付款、帳號、對外發送或其他工具動作權限。

檢查目前卡片及小型引用文件的完整性，不是重新讀取原始對話。格式與雜湊不能代替語義判斷；主責仍需確認內容是否記對，以及實際結果是否做對。

## 新增與修改

沿用目前卡片及 `rule_pipeline.bind_payload` 的內容審查，不需要為日常維護補齊歷史原話。`rule_provenance` 中的 `quote`、`sources`、`scope_basis` 是選用備查資料；新的 AI 整理可省略這三欄。

必要資料為 `version`、`key`、`kind`、`author`、`text`、完整 `scope`、`supersedes` 與內容綁定。`scope` 明列 level、project、case、actions、conditions、expiry、exceptions；未定期限為 `unspecified`，不等於永久授權。`kind: owner-directive` 仍表示卡片保留使用者的逐字文字，不能把 AI 改寫署為使用者。

```python
document = {
    "version": 1, "key": "project-work", "kind": "ai-derived", "author": "codex",
    "text": "已核對的現行要求。",
    "scope": {"level": "project", "project": "C:/project", "case": None,
              "actions": ["*"], "conditions": [], "exceptions": [],
              "expiry": "unspecified"},
    "supersedes": [],
}
document = rule_pipeline.bind_payload(document, card_text, reviewer="codex",
    rationale="已檢視目前意思、範圍、例外與實際使用欄位。")
```

AI 名稱用實際作者，不宣稱使用者逐字核准 AI 文字。取代其他主題或範圍時，保存舊 key、舊範圍、審查理由與雙向指標；原話不是必填。原文與歷史可原地留存或指向既有冷存，不複製完整大型對話。

保存事件與發布規則分開。自動捕捉的真人原句、來源檔案與行號或訊息身分、原始時間及 cwd 範圍，留在 `_drafts/captured_pending`，明標 `capture_review: UNREVIEWED`。`memsearch` 僅對這個草稿子目錄開放明確搜尋，結果標為未審事件、無授權效力；其餘私有目錄仍不進索引。貼上的助理文字不當作 owner 原句，問答情境中的助理題目另標作者。

未審事件不進主動喚回、授權、動作提醒、Stop／PreTool 規則或原生設定。執行走訪仍排除 `_drafts`，規則 projection 也拒絕此事件區；搜尋命中不等於核准。發布至 grants／corrections／rulings 或其他規則卡仍須通過現行內容、作者、範圍與正文綁定審核，不能只改 verified 或搬檔。已審內容可正常轉正，不以找不到原話為理由要求再次批准。一般非規則記憶維持自己的型別與維護程序，不因這項調整新增規則欄位要求。

Claude 與 Codex 共用捕捉核心；UserPromptSubmit、PreToolUse、Stop 從 transcript 增量讀取 `source_record` 的 U／Q 真人訊息，以訊息身分去重。第一次接上既有長對話只讀末段，較早的原話可用 harvest 回放；工具結果、壓縮摘要及重複的 Codex event stream 不作為 owner 發言。舊 captured_pending 不刪、不自動轉正；索引重建後即可查證，後續逐張內容審核再走既有轉正流程。

## 常駐生成段

`core-gen` 與全域、專案 `sync` 共用精簡格式：正文照抄，AI 作者與已審狀態集中在段首；不同作者及未審狀態另註。範圍不再列 JSON，改成每條至多一行「適用：…」：保留具體動作、條件、特定例外及案例／期限限制；萬用動作、通用的高優先系統／開發者指令例外，以及本文逐字涵蓋的內容不重複列出。不猜測跨語言的語意等價；具體動作會限縮適用時機，因此保守保留。共用專案只在段首列一次。來源目錄及檔名規則留一行指標，CORE 檔名例外才逐條註明；舊卡逐條標 `UNVERIFIED`，說明只寫一次。

逐條 HTML 註解改為整段一個 `epitype-rules` 雜湊。條目邊界、來源身分、完整範圍及作者保存在 `core-gen` 核准包的 `recovery` 欄位；`sync --apply` 則先保存至 `~/.epitype/host_rule_recovery/<hash>.json`，再寫宿主檔。這些小型狀態檔也供備份版本讀回，不是每次開場的載入內容；保留對應版本時一併保留。比對模式不寫這些狀態檔。

壞卡需沿用舊條文時，先核對狀態檔雜湊及該宿主整段文字，再按原邊界讀回，避免把正文內的標題或項目符號當成新規則。範圍直接讀取通過驗證的卡片；卡片已損壞時才沿用狀態檔中的上次核准快照，不從「適用」白話反推機器範圍。缺少或不符的狀態檔不能可靠讀回，該次同步保留宿主檔並回報原因；卡片有效時可直接重新生成，`sync --apply` 即使正文已一致也會補回狀態檔。舊的逐條標記與 JSON 範圍格式仍可讀回與遷移。pretooluse、stop、recall 及原生宿主設定一律使用卡片 scope，漂移檢查仍比對實際完整文字，位元組上限仍按實際宿主視圖計算。

## 兩種結果

`epitype rule-source <card>` 預設只檢查現行內容。成功為 `REVIEWED`，並明列 `source_audit_status: NOT_REQUESTED`；不聲稱讀過歷史原話。日常喚回、生成、Stop、PreTool、壓縮恢復、原生設定及夜間整理沿用這條路徑，不常態注入備查引句。

確實需要追溯時使用：

```powershell
epitype rule-source --audit-originals <card>
```

這條明確的唯讀查證保留原始 JSONL／ZIP／zst 的紀錄位置、角色、session、訊息身分、雜湊及必要的最早作者檢查。結果 `VERIFIED` 只證明該次原始來源核對通過；不是語義正確或動作授權。缺少選用備查資料的卡片可以通過現行內容檢查，而原文查證如實回報未能證明；不能偽造歷史來源補綠燈。

## 舊卡與原生設定

既有原話及來源指標保留備查；不為導入此流程全庫重寫或追查歷史。舊固定基準仍只證明目前內容與已保留基準相符，不證明使用者作者。實際內容、執行欄位或取代鏈有錯時修正，不以來源缺失混同為規則錯誤。

原生設定的身分、ownership-state、明確路徑、scope 與值衝突檢查維持。已審空清單可撤回原卡所擁有的設定；來源日常未回查不是撤回或擴權理由。`rule_source_baseline` v1/v2 與 `native_paths` 的完整性和原生身分保護仍有效。
