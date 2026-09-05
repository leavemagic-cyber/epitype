# Epitype

[English](README.md)

Epitype 是 CLI agent 的記憶治理層。

它沿用 host 的原生記憶作為儲存正本，再補上卡片結構、喚回時點、動作閘與驗證證據，讓已記住的規則能影響後續決策與工具動作。

AI 即使找回正確事實，仍可能違反隨附的規則。Epitype 處理的正是這段落差：

- 已被取代的決定，不該再以現行規則出現；
- 從事故得到的教訓，應在相關工具動作前抵達；
- 使用者給過的權限，必須能追溯到原話與本人；
- 記憶管線失效時，不能只剩沉默。

Epitype 目前支援 Claude Code 與 Codex，只使用 Python 標準函式庫，不需要另架雲端記憶服務。

## 運作方式

Epitype 把同一組原生 vault 接到四個 host 事件：

| 事件 | Epitype 的動作 |
|---|---|
| `SessionStart` | 記憶索引或工作帳本存在時，在大小上限內注入內容。 |
| `UserPromptSubmit` | 在共用輸出預算內，從每個已解析的 vault 取回最多五張相關卡片。簡短的 owner 授權語句會逐字保存、去重並立即進索引；捕捉當下不替原話加上解釋。 |
| `PreToolUse` | 用工具與輸入比對傷疤卡 trigger。命中時回傳有界拒絕、較安全的做法與一列稽核紀錄。 |
| `PreCompact` | 在 context 壓縮前，從 transcript 尾端製作小型復原地圖。 |

注入的記憶只是參考資料，不能推翻 system 或 developer 指令、繞過 host 權限，也不能自行授予工具操作權。每次 hook 輸出最多 10 KiB，執行超過十秒就 fail open，避免記憶層卡住宿主流程。

## 不只把內容找回來

### 現行決定只有一份

決策卡使用穩定的 `decision_key`，並記錄 `active` 或 `superseded` 狀態、生效時間與決定來源。每個 key 應只有一張 active 卡。`query`、`recall` 與 prompt hook 預設排除已被取代的卡，但保留歷史來源；只有在查沿革時才明確加上 `--include-superseded`。

### 傷疤可以攔下動作

傷疤是由實際事故產生的規則。適合攔截的卡片加上 `trigger.tool`、`trigger.input` 與可執行的 `advice` 後，就能成為窄範圍動作閘。命令比對預設只看可執行位置與未加引號的引數，所以引號字串、註解或 heredoc 內文出現觸發詞時不會誤攔；需要逐字全文比對的卡片可明確選用 full-text 模式。

### 安裝沿用原生記憶

安裝器只 merge 帶 Epitype 標記的項目，沿用偵測到的原生 vault，修改既有 host 檔前先備份。穩定 shim 讓 repo 搬家時不必重寫每個 host 註冊。解除安裝只移除 Epitype 擁有的註冊與設定，原生記憶和 vault 卡片會保留。

### 失敗會留下證據

索引不存在、索引過期、shim 故障、卡片格式錯誤與寫鎖競爭都有不同結果。hook 無法安全完成時會 fail open；installer doctor 也會把已記錄的 shim 下線事件報出來，不會把沒有聲音誤判成健康。

## 快速開始

需求：Python 3.11 以上，以及支援 hook 的 Claude Code 或 Codex。

安裝：`pip install epitype`。`epitype` 統一提供安裝、搜尋、lint、筆試與診斷工具；`epitype-graft` 保留為安裝器的相容別名。

npm 上的 `@hungyu/epitype` 只是指回這個 Python 專案的路標套件（npm 判裸名 `epitype` 與既有套件過於相似，不接受）。

先預覽預計變更：

```powershell
epitype install --dry-run
```

確認輸出只包含預期的 host 與路徑，再安裝並跑合成體檢：

```powershell
epitype install
epitype doctor
```

安裝器會偵測既有原生 vault；找不到時才建立空的 fallback vault。重新安裝會保留已策展的 vault 清單。若確定要改採最新偵測結果，先執行 `epitype vaults --resync --dry-run`，確認後再拿掉 `--dry-run`。

### 核准 Codex hooks

Codex 的 hook 註冊與 hook 信任是兩件事。安裝後必須檢查真實信任狀態：

```powershell
epitype trust
```

若任何 Epitype 項目顯示 `UNTRUSTED`、`DISABLED` 或 `MODIFIED`：

- 終端機介面輸入 `/hooks`，面板出現後按 `t` 信任全部，再按 `esc`。
- Desktop app 開啟 **hooks need review** 或 **Hooks** 面板，核准標記為 Epitype 的 `SessionStart`、`UserPromptSubmit`、`PreToolUse`、`PreCompact` 四筆項目。

核准後再跑一次檢查。只有印出 `CODEX TRUST: PASS 4/4` 才表示 Codex 端可執行。`doctor` 驗證的是註冊與合成執行，不能取代這項信任檢查。

### 選擇 vault 版型

從 repo 內的版型開始：

| 版型 | 適用情境 |
|---|---|
| [`minimal`](templates/minimal/) | 單人、單機。 |
| [`team`](templates/team/) | 多人共用 vault，使用統一寫鎖契約。 |
| [`power`](templates/power/) | 完整配置，包含 census 與 exam-ready 目錄。 |

## 搜尋本機 vault

先建立 vault 的本機 FTS 索引，再用關鍵詞或自然語言 prompt 查詢：

```powershell
epitype search build C:\path\to\vault
epitype search query 關鍵詞 --vault C:\path\to\vault
epitype search recall "自然語言提示" --vault C:\path\to\vault
```

資料庫位於 `<vault>/.epitype/memory_fts.sqlite3`，Git 會忽略它。只有 `build` 會建立原本不存在的索引；既有索引過期時會增量更新，缺少索引與合法的零結果則有不同回覆。

## 驗證這份 checkout

在 repo 根目錄執行公開可重跑的檢查：

```powershell
python tests/run_all.py
python tests/privacy_lint.py
python exam/exam_runner.py --strict
```

`tests/run_all.py` 目前執行 20 組元件 selftest，涵蓋核心工具、hook adapter、套件介面、安裝器、筆試引擎與隱私閘。repo 內的筆試題庫是小型合成樣本。本次發布另以嚴格模式通過 300 題行為題庫與 15 筆回顧種子；這兩份發布材料不包含在本 repo。

這些結果是防回歸證據，不代表未來每個 host 版本或每一種記憶失效都已涵蓋。

## 搬移或移除 Epitype

repo 搬家後，更新穩定 shim 的目標並重跑 doctor：

```powershell
epitype relocate --to C:\新的\repo\路徑
```

解除安裝前先預覽：

```powershell
epitype uninstall --dry-run
epitype uninstall
```

手動還原備份前，先讀 [解除安裝說明](docs/UNINSTALL.md)。

## 限制

- hook 只能治理 host 有提供的事件與工具；直接讀檔不會經過 Epitype 的現行決定過濾。
- 執行時間與輸出上限要求 Epitype 選擇內容，不會在每個 prompt 塞入整座 vault。
- 動作閘的準確度取決於傷疤 trigger 與 advice。卡片格式有誤時會 fail open，不接管 host。
- 目前測過的 host 邊界是 Claude Code 與 Codex；host 升級後仍需重新做整合驗證。
- 隨箱測試使用合成資料，驗的是行為與失敗處理，不是長期實地成效。

## 文件

- [架構說明](docs/ARCHITECTURE.md)：記憶分層、四條檢索路、決策卡、傷疤生命週期與權威規則。
- [失敗模式](docs/FAILURE_MODES.md)：病象、對治與驗證邊界。
- [解除安裝](docs/UNINSTALL.md)：依所有權移除與備份指引。
