# 回合內工具順序

```yaml
require_tool_before:
  - "Bash::inspect => Bash::mutate"
pair_tools:
  - "open_browser => close_browser"
audience: root
```

每個 selector 是工具名，選填 `::` 後的參數字面片段；`=>` 前是先做／開啟，後是後做／關閉。欄位採清單；工具等價沿用 Claude Bash／Codex exec_command 對照。`audience` 為 root、sub、all，空值維持 all。只在 Stop 讀得到本回合 owner 起點時檢查，缺起點不猜。

這是每回合的字面呼叫先後，不能證明工具成功、同一目標被讀／關閉或跨回合的身分配對。工具失敗也曾被呼叫，沒有成功資訊的卡不得宣稱驗證了結果。同一次 shell 呼叫不證明指令內先後。

三張獨立順序卡（inspect→mutate、backup→replace、test→commit）的測試在兩宿主各驗證該放行與該擋下；配對與主／子範圍另測。`tests/tool_sequence_regression.py` 3/3，既有 turn_check 46/46、card_lint 56/56。

以下至「成功回條與同一目標」之前，是初次驗收的歷史結果；其產品缺口已由後續同目標完成事實機制修復，最新驗證見文末。初次字面欄位不能降低原本要求。

2026-10-01 已讀七天窗口的全部 667/667 份可選紀錄，得到 3,097 個回合、79,137 筆紀錄中的工具呼叫；另有 29,294 個包裝內字面指令候選，執行狀態未驗證。範圍是台北時間 09-25 00:00 至 10-02 00:00；含尚未走完的當日，後續列持續增加。這是完整讀取證據，**不是三張真實順序卡零誤擋的驗收**。

逐張重新核對可轉候選後，讀具名檔、主 checkout status、搬移前掃消費端、刪除前對 worktree、Swift manifest 等仍需要同一目標與成功結果。现有每回合資料只證明叫過工具；Codex 包裝中的候選也不能冒充執行成功。直接換成 Read→Edit 或 git status→merge 會把讀錯檔／查錯工作樹當作合格，或把跨回合既有證據當作欠缺。因此本批不將這些卡降成較弱契約；三張既有卡轉用且七天零誤擋的 E 驗收尚未達成。

下一個可執行唯讀檢查：以這批全檔紀錄的 tool_use_id／call_id 對上工具成功結果，再加入目標路徑與工作樹身分對照，逐例人工檢查三張候選。退出條件是各有可驗證的真放行／真擋、七天所有命中逐例審查零誤擋。單純欄位測試或零命中不算通過。

## 成功回條與同一目標（2026-10-01 續修）

require_tool_before 可使用 @file.read => @file.change、@git.status => @git.main_update、@git.worktrees => @directory.delete。這些關係比較正規化目標及成功完成時間；讀別檔、查 side checkout、工具失敗或尚未完成均不能抵用。自製安裝不需 owner 的固定路徑。檔案讀取可沿用本場已知的自有修改；主樹每次更新仍須前一次更新之後的新 status。

既有 PreToolUse 記錄開始，PostToolUse 才確認完成；只保存目標、repo、工具 ID、內容不可逆摘要及成功狀態，不保存指令或檔案內容。這是完成事實與因果順序，沒有名額或配對的即時計数。訊號遺漏標 UNVERIFIED，不形成永久欠帳。Stop 只查目前 owner 回合；Codex 以官方 turn_id 讀真內層回條，無須把 JavaScript 字面候選當作执行。

官方依據：[Claude hooks](https://code.claude.com/docs/en/hooks#posttooluse) 的 PostToolUse 是成功事件；[Codex hooks](https://learn.chatgpt.com/docs/hooks) 包含非零 shell 結果及 code mode 真內層工具，所以必須各自判讀。兩邊使用既有事件與 matcher。[Git worktree](https://git-scm.com/docs/git-worktree) 的 gitdir／commondir 區分主／側樹，porcelain inventory 提供具體工作樹路徑。

文字摘要／評斷、何謂大量變更，以及複雜腳本未提供可驗證的內層結果，仍需人的判斷或標未驗證；不擅自設數字門檻。上述欄位不替代原卡全文，也不代表回條能證明工具結果內容正確。

新場才從完整起點記錄；resume／compact 不追造遺失紀錄。中途安裝、少了 Pre 回條或先前不透明程式，沒有先做證據時列 UNVERIFIED。已完成、同一目標的正向證據仍可放行。相同 typed 欄位在 PreToolUse 先檢查，避免操作後才補讀；Stop 繼續檢查實際完成結果。兩處共用同一個關係引擎和既有守衛快取，沒有新增宿主 matcher。

實際宿主接線另驗出：現有 PostToolUse matcher 只涵蓋瀏覽器，因此一般工具不會送完成回條。既有 Pre／Stop 現會從原生 transcript 補查已綁定的 call ID 與原參數摘要；只認相同 actor 的真正結果。Code mode 只接受單一、字面參數的 `text(await tools.exec_command(...))` 原生結果轉送，分支、多呼叫、變數與自行印出的成功字樣均不算執行證據。工作目錄取真正呼叫的 workdir，不能拿 hook 的 session cwd 冒充。查補最多讀最近 1 MiB，找不到完整來源便保持未驗證；夜間重放仍讀全檔。此相容解析按本機實際 call/output 格式驗證，不宣稱所有版本或所有 JavaScript 都可判讀。沒有改 hooks.json 或模板的 matcher。

Pre 的 `sequence_allow` 稽核列代表該次工具前置檢查真正放行；操作成功仍另看完成結果。它不算違規或擋下次數。

## 實際部署與重放結果（2026-10-01 最新）

具名檔修改、主 checkout 更新、刪目錄前核對 worktree 三張既有規則已轉用同目標欄位，原正文保留。真實 Codex 工具在本次自建 Git fixture 各產生一次 `sequence_allow`：Read 成功後修改同檔且重讀確認；主樹 status 成功後真正 FF；worktree inventory 成功後刪除自己的空目錄。合併、inventory、刪除均有成功完成回條；apply_patch 原生輸出缺少可證成功 metadata，故該筆完成事實仍 pending，成功改檔另由實際重讀確認。不得把 Pre 放行當操作成功。

完整已走完七日窗口為台北 09-24 00:00 至 10-01 00:00：803/803 文件，91,724 個實際呼叫，讀檔→修改 1,132 次正向放行、1,326 次證據不足、0 次判違規；五個放行樣本逐一核對同檔、同 actor 與成功完成先後。另兩條規則缺可識別歷史命中，不能以零命中聲稱零誤擋。62,637 個不透明程式呼叫與 721 個未知 Write 如實保留，沒有把字面候選當完成結果，也沒有追造過去缺失資料。新資料的三次真放行與回歸證據補上原先未接線的機制，歷史覆蓋上限仍須明列。

未完成的主樹更新會使舊 status 失效；新 nested linked checkout 未出現在舊 inventory 時不能抵用。獨立 `.git` 檔案指向另一 metadata 目錄，仍是自己的主 checkout；linked commondir 的非標準位置若不能確認主樹則保留未知。[Git repository layout](https://git-scm.com/docs/gitrepository-layout) 是 gitfile／commondir 的官方依據。
