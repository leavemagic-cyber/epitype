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

Epitype 把同一組原生 vault 接到五個 host 事件：

| 事件 | Epitype 的動作 |
|---|---|
| `SessionStart` | 對沒有原生載入索引的宿主回音短入口（在大小上限內），另外只出「有事要做」的行（卡片型別 FAIL、夢報錯／有待審候選／到期沒跑）。常駐內容一律不重送。 |
| `UserPromptSubmit` | 在共用輸出預算內，從每個已解析的 vault 取回最多五張相關卡片。簡短的 owner 授權語句會逐字保存、去重並立即進索引；捕捉當下不替原話加上解釋。 |
| `PreToolUse` | 寫檔閘：檔案寫入落盤前，先用現行裁定與卡片型別合約檢查要寫進去的內容。擋下時回傳那條裁定與一列稽核紀錄。 |
| `PreCompact` | 在 context 壓縮前，從 transcript 尾端製作小型復原地圖。 |
| `Stop` | 回合結束決策閘：回覆若再提已否決選項或再問已裁定的事就擋下。 |

注入的記憶只是參考資料，不能推翻 system 或 developer 指令、繞過 host 權限，也不能自行授予工具操作權。每次 hook 輸出最多 10 KiB，執行超過十秒就 fail open，避免記憶層卡住宿主流程。

### 捕捉：入庫，或只是提案

觸發詞命中只證明「這句話長得像裁定」，不證明有人核過，所以只有三個形狀模板放行自動
入庫——箭頭回覆而 owner 那半以短答開頭、句首就是糾正、明說是第一人稱在授權。其餘仍
被規則捕捉到的句子改寫成提案，落在 `<vault>/_drafts/captured_pending/YYYYMMDD/`：不進
索引、不被喚回，等人看過再說。兩種卡都帶 `provenance: auto-captured` 與
`verified: false`；轉正＝改成 `verified: true` 並補 `verified_by`／`verified_at` 再搬檔，
回放不會替你做這件事。

`verified: false` 的卡不是任何東西的依據：Stop 決策閘與寫檔閘讀的都是宣告
`decision_key` 的卡，捕捉卡從來不宣告這個。
喚回照樣端得出來，但掛的是「歷史捕捉」而不是現行裁定。細節與量測見
`docs/FAILURE_MODES.md` §32。

## 不只把內容找回來

### 現行決定只有一份

決策卡使用穩定的 `decision_key`，並記錄 `active` 或 `superseded` 狀態、生效時間與決定來源。每個 key 應只有一張 active 卡。`query`、`recall` 與 prompt hook 預設排除已被取代的卡，但保留歷史來源；只有在查沿革時才明確加上 `--include-superseded`。

### 傷疤，以及真正攔得住動作的是什麼

傷疤是由實際事故產生的規則：一個 `incident`（哪次踩到）加上可執行的 `advice`（改走哪條路）。卡片是被讀回一個回合的內容，不是拒絕——它擋不住任何一次工具呼叫，用字串樣式假裝擋得住只會同時製造誤擋與虛假的安全感。不可逆動作交給宿主自己的原生規則（Claude `permissions.deny`、Codex `execpolicy`），在呼叫發生前就拒絕。Epitype 只擋內容：下面的寫檔閘，與回合結束的 Stop 閘。

### 安裝沿用原生記憶

安裝器只 merge 帶 Epitype 標記的項目，沿用偵測到的原生 vault，修改既有 host 檔前先備份。穩定 shim 讓 repo 搬家時不必重寫每個 host 註冊。解除安裝只移除 Epitype 擁有的註冊與設定，原生記憶和 vault 卡片會保留。

### 整理會自己跑（夢）

離線整理批次不必等人想起來。預設 `dream.mode: piggyback`：開場時若距上次整理超過 `dream.interval_hours`（預設 24 小時），就起一個脫鉤的低優先權背景程序，開場本身不等它；lock 檔帶 pid 與時間，逾 30 分鐘視為死鎖可覆蓋，所以同一時間只會有一個。想用系統排程就 `graft install --dream nightly [--at HH:MM]` 註冊每日任務（`graft doctor` 顯示模式與上次完成時間，`graft uninstall` 反註冊），`--dream off` 則兩者都不做。背景那一趟只讀 vault，只寫 `<治理 vault>/.epitype/` 底下的審核包、狀態與 log，自己抓十分鐘時限，下一場開場用一行說明結果。最後一節是回饋檢討包：被 owner 事件、閘門擋下或考題失敗指到的卡各一列，列數滿門檻（`memspec.REVIEW_PACK_TRIGGER`，5）才提醒該開一場檢討；它不判斷、不改任何一張卡。全程不呼叫模型——整理的模型那半永遠手動，分享版不會偷跑你的模型額度。

### 失敗會留下證據

索引不存在、索引過期、shim 故障、卡片格式錯誤與寫鎖競爭都有不同結果。hook 無法安全完成時會 fail open；installer doctor 也會把已記錄的 shim 下線事件報出來，不會把沒有聲音誤判成健康。

`epitype gates <vault> [--since Nd|YYYY-MM-DD] [--json] [--by kind|decision|session|day]` 把 vault 的 `_GATE_LOG.jsonl` 唯讀整理成閘門實際擋下什麼的報告——依 kind、決策卡或傷疤卡、天、session 分，並列出同一 session 同一卡連擋 ≥3 次的疑似誤擋提示。

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
- Desktop app 開啟 **hooks need review** 或 **Hooks** 面板，核准標記為 Epitype 的 `SessionStart`、`UserPromptSubmit`、`PreToolUse`、`PreCompact`、`Stop` 五筆項目。

核准後再跑一次檢查。只有印出 `CODEX TRUST: PASS 5/5` 才表示 Codex 端可執行。`doctor` 驗證的是註冊與合成執行，不能取代這項信任檢查。

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

## 指令參考

`epitype <指令> --help` 會印出下表任一指令的完整選項。

### 日常會用

| 指令 | 做什麼 |
|---|---|
| `epitype doctor [--home HOME] [--dry-run] [--clear-shim-status]` | 對已安裝的 hook 註冊與 shim 執行做合成體檢；安裝後或懷疑哪裡壞了時執行。 |
| `epitype dream [vaults...] [--since SINCE] [--dry-run] [--scheduled] [--json]` | 唯讀離線整理盤點（缺別名、卡片 lint 結果、殭屍待辦、待審草稿、老化事件卡，以及未登記的口袋庫、草稿老化、混雜卡拆卡候選、超過設定上限的檔案、沒有決策卡承接的 owner 原話、與規則卡重組結果不一致的生成塊，以及把 owner 事件、閘門擋下與考題失敗對齊到卡上的檢討包），整理成一份編號審核包；本身不套用任何建議。排程模式是 config 的 `dream.mode`——`piggyback`（預設：開場時起一個脫鉤背景程序）、`nightly`（系統排程）、`off`；用 `epitype install --dream {piggyback,nightly,off} [--at HH:MM]` 切換（nightly 預設 `03:30`）。 |
| `epitype gates <vault> [--since Nd\|YYYY-MM-DD] [--json] [--by kind\|decision\|session\|day]` | 把 `_GATE_LOG.jsonl` 整理成閘門實際擋下什麼的報告，例如 `epitype gates C:\path\to\vault --since 2d`。 |
| `epitype cards <vault> [--strict] [--verbose] [--deep] [--json] [--fix-dates [--dry-run]]` | 依必填欄位檢查記憶卡。`--deep` 另加庫層級檢查：同一個 `decision_key` 只有一張現行卡、取代鏈完整、每張納管卡都在生成目錄與搜尋索引裡。`--fix-dates` 是唯一會寫檔的旗標：把推得的日期補成一行 `last_verified_at:`；先用 `--fix-dates --dry-run` 預覽會寫什麼。 |
| `epitype views <vaults...> [--force] [--json]` | 依卡片欄位重生可瀏覽的目錄：`_views/current.md`（現用卡，含完整現行決策清單）與 `_views/history/closed.md`（已結案專案與已取代決策）。永遠不寫 `MEMORY.md`；輸入指紋沒變就不重寫；同庫並行有鎖。說明見 [三個閱讀層級](docs/ARCHITECTURE.md#three-reading-levels)。 |
| `epitype core-gen <vaults...> --out FILE [--cap-bytes N] [--dry-run] [--check] [--json]` | 由 `type: rule` 卡組裝常駐核心塊：`floor` 依 `order` 編號、`resident` 依 `section` 分小節，每張卡核准過的 `text` 逐位元組照抄，並在庫內寫一份核准包。組出來超過上限、或生成層的卡缺 `approved_by`／`approved_at` 時拒絕寫出（回非零）。`--check` 只比對不寫檔，供漂移稽核使用。說明見 [核心生成](docs/ARCHITECTURE.md#core-generation-rule-cards--the-resident-block)。 |
| `epitype aliases {export,apply}` | `export` 把缺別名的卡片列成 JSON 工作清單；`apply` 把審核過的 `suggested` 別名寫回卡片，只新增不刪改。 |
| `epitype search {build,query,recall}` | 建立本機 FTS 索引，並用關鍵詞或自然語言查詢；詳見上方〈搜尋本機 vault〉。 |

### 維護／批次

| 指令 | 做什麼 |
|---|---|
| `epitype decisions [vault] [--audit] [--selftest]` | 唯讀掃描決策卡：每個 key 是否唯一、取代鏈是否完整、決定者欄位；`--audit` 列出非 `owner-explicit` 的現行決策。 |
| `epitype ledger append --ledger PATH --entry TEXT --evidence PATH::SUBSTRING [--check-only]` | 追加帳目前，先逐條確認每筆證據真的出現在指定檔案的 bytes 裡；`--check-only` 只驗證不寫入。 |
| `epitype capture-route <vault> [--audit] [--apply] [--home HOME] [--json]` | 用落點規則盤點一個庫裡自動捕捉的事件卡：卡屬於它 `cwd` 指到的專案庫，所以治理庫裡其實屬於別的庫的卡會列成 `MISROUTED <卡> -> <庫>`。`--audit` 唯讀；`--apply` 才真的搬（`os.replace`、同名加 `-2`、永不刪），並在卡的正文補一行歸戶註記。 |
| `epitype harvest [--inventory] [--docs DOCS] [--since SINCE] [--reevaluate DIR [--apply]] [--quarantine-drops [DIR]]` | 零模型回放捕捉規則到歷史 transcript 與文件，做第一次大整理的補課；也能用現行規則重新評斷草稿或 vault 自己的事件卡。 |
| `epitype token-meter [rollout] [--selftest]` | 讀 Codex rollout JSONL，印出最後一筆當前與累計 token 用量對照視窗大小。 |
| `epitype scar-census build` | 建立四層傷疤普查的機器生成視圖。 |
| `epitype compact-map build` | 建立有界的壓縮復原地圖，與 `PreCompact` 每場自動寫的是同一種。 |
| `epitype source SOURCE.jsonl [--find TEXT] [--role user\|assistant\|all] [--line N] [--offset BYTES] [--limit 1..8]` | 唯讀查原始訊息，列出角色、實體行號、雜湊及截斷／涵蓋範圍。指定 offset 時行號相對該位元位置。逐字查找不等於現行裁定或前提已受證據支持。 |
| `epitype pending <vault> [--max-age-days N] [--strict] [--json]` | 找殭屍待辦：有待辦標記、沒收尾字樣、沒有可跑的 `verify:`、且超過年齡門檻的行。 |
| `epitype exam [corpus] [--strict] [--selftest]` | 對行為題庫跑筆試引擎。 |
| `epitype trust [--home HOME]` | 檢查 Codex 真實的 hook 信任狀態；詳見上方〈核准 Codex hooks〉。 |
| `epitype install \| uninstall \| vaults \| relocate` | 安裝器、解除安裝、vault 重新偵測與 repo 搬遷；詳見上方〈快速開始〉與下方〈搬移或移除 Epitype〉。 |

## 驗證這份 checkout

在 repo 根目錄執行公開可重跑的檢查：

```powershell
python tests/run_all.py
python tests/privacy_lint.py
python exam/exam_runner.py --strict
```

`tests/run_all.py` 目前執行 32 組元件 selftest，涵蓋核心工具、hook adapter、套件介面、安裝器、筆試引擎與隱私閘。repo 內的筆試題庫是小型合成樣本。本次發布另以嚴格模式通過 300 題行為題庫與 15 筆回顧種子；這兩份發布材料不包含在本 repo。

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
- Epitype 擋的是內容而不是動作：它不會拒絕任何一條 shell 指令或讀檔。不可逆動作由宿主原生規則負責。卡片格式有誤時會 fail open，不接管 host。
- 目前測過的 host 邊界是 Claude Code 與 Codex；host 升級後仍需重新做整合驗證。
- 隨箱測試使用合成資料，驗的是行為與失敗處理，不是長期實地成效。

## 文件

- [架構說明](docs/ARCHITECTURE.md)：記憶分層、四條檢索路、決策卡、傷疤生命週期與權威規則。
- [失敗模式](docs/FAILURE_MODES.md)：病象、對治與驗證邊界。
- [解除安裝](docs/UNINSTALL.md)：依所有權移除與備份指引。
