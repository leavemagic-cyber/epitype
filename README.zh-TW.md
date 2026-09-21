# Epitype

[![PyPI](https://img.shields.io/pypi/v/epitype.svg)](https://pypi.org/project/epitype/)
[![Python](https://img.shields.io/pypi/pyversions/epitype.svg)](https://pypi.org/project/epitype/)
[![License: MIT](https://img.shields.io/github/license/leavemagic-cyber/epitype.svg)](LICENSE)

[English](README.md)

規則明明寫在 `CLAUDE.md` 裡，它照樣做成別的樣子。

三天前才裁定過的事，今天它拿你已經換掉的那個版本回答你。compact 一次之後，寫在指令檔裡的規則就像沒看過。

Epitype 就是為了這件事做的一層小東西。它接 Claude Code 和 Codex 原本就有的 hook，你原本的記憶檔放在哪裡就繼續放在哪裡，它補上那些檔案自己做不到的部分：該用到的那張紙，在該用到的那一刻送到它面前；不該用的那張，擋在外面。

只用 Python 標準函式庫。不必註冊服務，自己不呼叫模型，也不用付錢。

## 三十秒看到它動起來

裝好之後，放五張通用規則進你的記憶庫：

```powershell
pip install epitype
epitype install
epitype starter
```

接著叫你的代理跑 `git add -A`。這次呼叫在 git 看到它之前就被擋掉：

```text
🛑 傷疤卡（Stage explicit paths, not the whole tree）：這次 Bash 同時含有
「git add -A」——Run git status first, then name each path you actually changed
```

再讓它用 `That should fix it.` 收尾。這一則不會送到你面前，會被退回重寫：

```text
⚖ 不要說「That should fix it」，請改寫。（No hedged completion：should work now
is a guess wearing the clothes of a result）
```

帶著證據的那一則——`I ran the suite: 74/74. Pushing now.`——原樣通過。

這五張起手卡的內容是英文的（規則本身、要擋的字眼都是英文），外框的說明文字才跟著你的
語言設定走，所以上面看到的是英文句子配中文外框。它們就是記憶庫裡的 Markdown：整張改成
中文、刪掉、或用 `epitype starter --remove` 把你沒動過的那幾張收回去，都可以。

## 你實際會在哪裡看到它

週一你否決掉的做法，週四它又提一次。Stop 閘把那段回覆攔下來，附上當初結掉這題的那條裁定，連同日期和是誰裁的。

一次檔案寫入正要把違反現行規則的內容落盤。寫檔閘在落盤前擋下來，留一列稽核，之後你查得到擋了什麼、擋了幾次，以及那條規則是不是擋過頭了。

做到一半 context 被壓縮。下一輪開場會拿到一份壓縮前寫好的復原地圖路徑，讓它去讀當時真正說過的話，而不是自己重建。

這三件事都不是多開一個地方存東西。它們跑的都是你 vault 裡已經有的那些紙。


## 安裝

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


## 這是給誰的

如果你已經在用 Claude Code 或 Codex、手上有一份自己在維護的 `CLAUDE.md` 或 `AGENTS.md`、看過 agent 忘記或重開你早就裁定過的事，而且希望留下「什麼被擋下來」的紀錄，那值得試。

如果你剛開始用 CLI agent、還沒累積出需要治理的規則，或者你真正想要的是一道「擋得住有心繞過的人」的安全邊界，那先不急。後面那件事是宿主的職責：Claude Code 有 `permissions.deny`，Codex 有 `execpolicy`。Epitype 擋得住「傷疤卡指名的那幾個字面片段同時出現」的工具呼叫，但把同一個指令換個等價寫法就過得去——它是防止重犯已經記錄過的錯誤，不是防人。

## 運作方式

Epitype 把同一組原生 vault 接到五個 host 事件：

| 事件 | Epitype 的動作 |
|---|---|
| `SessionStart` | 只出「有事要做」的行（卡片型別 FAIL、順手補中文別名、夢報錯／有待審候選／到期沒跑）。壓縮之後那一場，另外把下面那份地圖的路徑交回去，讓它自己去讀壓縮前的原話。沒事要做的那一場整段不注入；常駐內容一律不重送，短入口索引也一樣，那由宿主自己從 `CLAUDE.md`／`AGENTS.md` 載入。 |
| `UserPromptSubmit` | 在共用輸出預算內，從每個已解析的 vault 取回最多五張相關卡片——只端卡片，逐字捕捉的原話檔搜得到但不注入。簡短的 owner 授權語句會逐字保存、去重並立即進索引；捕捉當下不替原話加上解釋。 |
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
喚回一個字都不端：捕捉卡是最底層的原話，AI 要用到時自己 `memsearch` 搜出來，
UserPromptSubmit 只端卡片。細節與量測見 `docs/FAILURE_MODES.md` §32 與 §41。

## 不只把內容找回來

### 現行決定只有一份

決策卡使用穩定的 `decision_key`，並記錄 `active` 或 `superseded` 狀態、生效時間與決定來源。每個 key 應只有一張 active 卡。`query`、`recall` 與 prompt hook 預設排除已被取代的卡，但保留歷史來源；只有在查沿革時才明確加上 `--include-superseded`。

### 傷疤，以及真正攔得住動作的是什麼

傷疤是由實際事故產生的規則：一個 `incident`（哪次踩到）加上可執行的 `advice`（改走哪條路）。多數卡片是被讀回一個回合的內容，什麼都不拒絕。

卡片也可以拒絕，形式有三種而且都很窄。`forbidden` 樣式讓回合在說出 owner 裁定過不能說的話時結束不了；`require_when` 配 `require_text` 讓「宣稱」在缺了該附的證據時結束不了；`guard_tool` 配 `guard_all_of` 在一次工具呼叫的文字**同時含有卡片指名的每一個字面片段**時拒絕它——沒有正則、不解析 shell、不猜指令的意思。

最後這一項 2026-09-09 曾被移除，2026-09-16 恢復：當初移除的前提（交給宿主原生規則）實測不成立——Claude 的 Bash 權限樣式是位置比對、沒有 AND 運算子，九類危險裡四類搬得過去、五類根本寫不出來。字面連言會往「多擋」的方向偏，那是安全的方向；它是**防止重犯已經記錄過的錯誤，不是安全邊界**。同一個指令換個等價寫法就過得去，這是設計上接受的。

語意判斷與真正不可逆的動作仍然交給宿主自己的原生規則（Claude `permissions.deny`、Codex `execpolicy`），在呼叫發生前就拒絕。

### 安裝沿用原生記憶

安裝器只 merge 帶 Epitype 標記的項目，沿用偵測到的原生 vault，修改既有 host 檔前先備份。穩定 shim 讓 repo 搬家時不必重寫每個 host 註冊。解除安裝只移除 Epitype 擁有的註冊與設定，原生記憶和 vault 卡片會保留。

### 整理會自己跑（夢）

離線整理批次不必等人想起來。預設 `dream.mode: piggyback`：開場時若距上次整理超過 `dream.interval_hours`（預設 24 小時），就起一個脫鉤的低優先權背景程序，開場本身不等它；lock 檔帶 pid 與時間，逾 30 分鐘視為死鎖可覆蓋，所以同一時間只會有一個。想用系統排程就 `graft install --dream nightly [--at HH:MM]` 註冊每日任務（`graft doctor` 顯示模式與上次完成時間，`graft uninstall` 反註冊），`--dream off` 則兩者都不做。背景那一趟自己抓十分鐘時限，會寫三個地方：`<治理 vault>/.epitype/` 底下的審核包、狀態、log 與閘門健康度；索引漂掉時的治理庫 `MEMORY.md`；以及——這一項值得知道，因為它是無人看著的時候發生的——`~/.claude/CLAUDE.md` 與 `~/.codex/AGENTS.md` 裡標記之間的區塊，所以你改了一張規則卡，隔天早上代理讀到的就是新的，不必記得跑任何指令。只動標記之間，寫之前先備份，`epitype sync --remove`（或 `graft uninstall`）可以整段拿回去。順路收割新素材成草稿，已經入庫的卡一張都不動，下一場開場用一行說明結果。最後一節是回饋檢討包：被 owner 事件、閘門擋下或考題失敗指到的卡各一列，列數滿門檻（`memspec.REVIEW_PACK_TRIGGER`，5）才提醒該開一場檢討；它不判斷、不改任何一張卡。全程不呼叫模型——整理的模型那半永遠手動，分享版不會偷跑你的模型額度。

### 裝好的樣子長這樣

`epitype doctor` 會餵一則合成事件給每個掛鉤，然後報誰回應了：

```text
HOSTS: claude, codex
SHIM RESOLUTION: PASS 5/5 repo_root=C:\Epitype\repo
REGISTRATION claude: PASS 6/6
REGISTRATION codex: PASS 6/6
HOOK SessionStart: PASS (515 ms)
HOOK UserPromptSubmit: PASS (369 ms)
HOOK PreCompact: PASS (297 ms)
HOOK PreToolUse: PASS (301 ms)
HOOK Stop: PASS (287 ms)
HOOK SubagentStop: PASS (268 ms)
HEALTH PASS 6/6
```

### 失敗會留下證據

索引不存在、索引過期、shim 故障、卡片格式錯誤與寫鎖競爭都有不同結果。hook 無法安全完成時會 fail open；installer doctor 也會把已記錄的 shim 下線事件報出來，不會把沒有聲音誤判成健康。

`epitype gates <vault> [--since Nd|YYYY-MM-DD] [--json] [--by kind|decision|session|day]` 把 vault 的 `_GATE_LOG.jsonl` 唯讀整理成閘門實際擋下什麼的報告——依 kind、決策卡或傷疤卡、天、session 分，並列出同一 session 同一卡連擋 ≥3 次的疑似誤擋提示。


## 裝好之後

### 核准 Codex hooks

Codex 的 hook 註冊與 hook 信任是兩件事。安裝後必須檢查真實信任狀態：

```powershell
epitype trust
```

若任何 Epitype 項目顯示 `UNTRUSTED`、`DISABLED` 或 `MODIFIED`：

- 終端機介面輸入 `/hooks`，面板出現後按 `t` 信任全部，再按 `esc`。
- Desktop app 開啟 **hooks need review** 或 **Hooks** 面板，核准標記為 Epitype 的 `SessionStart`、`UserPromptSubmit`、`PreToolUse`、`PreCompact`、`Stop` 五筆項目。

核准後再跑一次檢查。只有印出 `CODEX TRUST: PASS 5/5` 才表示 Codex 端可執行。`doctor` 驗證的是註冊與合成執行，不能取代這項信任檢查。

### 顯示語言

閘門給你看的每一句話有英文與繁體中文兩種。全新安裝會在 `~/.epitype/config.json` 寫入
`"language": "en"`；作業系統語系是中文時寫 `"language": "zh-TW"`。改那個欄位就換語言，
或用環境變數 `EPITYPE_LANG`（`en` 或 `zh-TW`）覆寫單次執行，它比設定檔優先。設定檔沒有
這個欄位＝照舊 `zh-TW`。`epitype doctor` 會在 `LANGUAGE:` 那一行印出目前生效的值。這個開關
只換顯示字：樣式、欄名、閘門拿去比對的東西兩種語言完全一樣，換語言不會改變擋或不擋。

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
| `epitype views <vaults...> [--force] [--json]` | 依卡片欄位重生可瀏覽的目錄：`_views/current.md`（現用卡，含完整現行決策清單）與 `_views/history/closed.md`（已結案專案與已取代決策）。永遠不寫 `MEMORY.md`；輸入指紋沒變就不重寫；同庫並行有鎖。說明見 [四個閱讀層級](docs/ARCHITECTURE.md#four-reading-levels)。 |
| `epitype core-gen <vaults...> --out FILE [--cap-bytes N] [--dry-run] [--check] [--json]` | 由 `type: rule` 卡組裝常駐核心塊：`floor` 依 `order` 編號、`resident` 依 `section` 分小節、只給單一宿主的卡另立宿主區，每張卡核准過的 `text` 逐位元組照抄，並在庫內寫一份核准包。組出來超過上限、或生成層的卡缺 `approved_by`／`approved_at` 時拒絕寫出（回非零）。`--check` 只比對不寫檔，供漂移稽核使用。說明見 [核心生成](docs/ARCHITECTURE.md#core-generation-rule-cards--the-resident-block)。 |
| `epitype aliases {export,apply}` | `export` 把缺別名的卡片列成 JSON 工作清單；`apply` 把審核過的 `suggested` 別名寫回卡片，只新增不刪改。 |
| `epitype search {build,query,recall}` | 建立本機 FTS 索引，並用關鍵詞或自然語言查詢；詳見上方〈搜尋本機 vault〉。 |

### 維護／批次

| 指令 | 做什麼 |
|---|---|
| `epitype decisions [vault] [--audit] [--selftest]` | 唯讀掃描決策卡：每個 key 是否唯一、取代鏈是否完整、決定者欄位；`--audit` 列出非 `owner-explicit` 的現行決策。 |
| `epitype ledger append --ledger PATH --entry TEXT --evidence PATH::SUBSTRING [--check-only]` | 追加帳目前，先逐條確認每筆證據真的出現在指定檔案的 bytes 裡；`--check-only` 只驗證不寫入。 |
| `epitype capture-route <vault> [--audit] [--apply] [--home HOME] [--json]` | 用落點規則盤點一個庫裡自動捕捉的事件卡：卡屬於它 `cwd` 指到的專案庫，所以治理庫裡其實屬於別的庫的卡會列成 `MISROUTED <卡> -> <庫>`。`--audit` 唯讀；`--apply` 才真的搬（`os.replace`、同名加 `-2`、永不刪），並在卡的正文補一行歸戶註記。 |
| `epitype harvest [--inventory] [--docs DOCS] [--since SINCE] [--drafts-only] [--reevaluate DIR [--apply]] [--quarantine-drops [DIR]]` | 零模型回放捕捉規則到歷史 transcript 與文件，做第一次大整理的補課；也能用現行規則重新評斷草稿或 vault 自己的事件卡。 `--drafts-only` 時，找到的東西一律留在待審草稿區。 |
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

`tests/run_all.py` 目前執行 50 組元件 selftest，涵蓋核心工具、hook adapter、套件介面、安裝器、筆試引擎與隱私閘。repo 內的筆試題庫是小型合成樣本。本次發布另以嚴格模式通過 330 題行為題庫與兩份種子回顧；這些發布材料不包含在本 repo。

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

## 你的資料會去哪裡

哪裡都不會去。Epitype 不連網：整個套件裡沒有匯入 `urllib`、`http`、`socket` 或任何
HTTP 客戶端，也沒有任何回傳、帳號或金鑰。卡片是你自己目錄裡的 Markdown 檔；搜尋索引
是一個本機 SQLite 檔，刪掉可以重建。擋下紀錄只記規則名與時間，不記訊息內容。

## 跟別的工具怎麼分

- **[claude-mem](https://github.com/thedotmack/claude-mem)** 把對話摘要起來，讓代理記得更多。
  Epitype 不做摘要；它保管你寫下的裁定，並在代理說出違反它的話時把那一則退回。
- **[claudekit](https://github.com/carlrannaberg/claudekit)** 這類掛鉤工具箱給你零件自己組。
  Epitype 給的是一層做好的東西：卡片進去，擋下來出來。
- **CLAUDE.md／AGENTS.md 範本包**是給代理更多字讀。Epitype 從你的規則卡生成那個常駐區塊，
  然後在回合結束時去查那條規則有沒有真的守住。

三者不衝突：你照樣可以同時跑一個摘要型的記憶工具。

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

## 專案狀態

目前 v1.6.0，2026-09-22 發布。發布時機看一批修正什麼時候齊，不是固定日期。Issue 都會看，開 issue 是讓修正往前排最快的方式。
