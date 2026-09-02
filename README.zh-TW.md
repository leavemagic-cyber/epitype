# Epitype

Epitype 是蓋在 CLI agent 原生記憶之上的治理層：它不追求存得更多，而是讓已存下的內容真正約束行為。

它沿用原生記憶，不另造一套記憶服務，也不取代或停用 host 原有機制。

> 發布狀態：尚未到 v1.0。現階段可用合成資料自驗，且已交付讀取時預設排除已取代決策；發布筆試仍未完成。

## 問題不只在記不記得

- AI 最需要查記憶的那一刻，往往正是它最不覺得需要查的時候。把搜尋做成選用工具，或只在 session 開頭塞一次內容，都不等於行為紀律。
- 寫入端可能知道某項決定何時失效，讀取端卻仍把新舊版本一起送到模型眼前。資料存對了，不代表決策當下看對了。
- 規則即使已經存下，仍只是 context。若沒有接到動作閘，AI 可能口頭承認規則，下一步照樣違反。
- 本次賽道深析所涵蓋的 benchmark 都在量問答或召回，沒有量 AI 行動時是否遵守記住的規則。

## Epitype 做什麼

1. **逐動作、依情境強制喚回。** 每個受涵蓋的工具動作發生前，hook 會用當下工具與輸入比對傷疤 trigger；prompt 階段則另做相關卡片檢索。這是動作面上的情境選擇，不是只在 session 開頭灌一次記憶。
2. **決策取代契約。** 決策卡有穩定鍵、狀態、生效時間與裁定來源；lint 會拒絕同一鍵出現多張 active 卡，若一張都沒有則提出警告。`query` 與 `recall` 預設排除 `status=superseded` 的卡、保留索引中的 provenance；若繼任卡不在結果中，另回傳現行決定導向行。`--include-superseded` 才會為考古刻意取回新舊兩版，prompt 階段的 hook 喚回則直接繼承安全預設。
3. **由傷疤驅動的攔截。** deny 條件來自帶有明確 trigger 的累積教訓卡，而不只是「有一個 hook」。命中後會給出有界拒絕、較安全的替代路與稽核紀錄。Epitype 的差異是把記憶接上攔截閘，不是宣稱發明 hook 或 deny。
4. **量行為，不只量想不想得起來。** 現有元件都附合成行為 selftest；v1.0 則以隨箱筆試引擎為發布閘。只有 recall 分數，不能當成發布證據。
5. **原生優先。** 安裝只 merge 有 Epitype 標記的 hook，不會故意停用原生記憶。合成的「安裝後 host 檔未再變動」往返測試可把註冊檔恢復成原始 bytes，vault 保留；若安裝後另有合法變更，解除安裝會保留新變更，而不是拿舊備份硬蓋回去。
6. **支援的 CLI 共用一份記憶與一套 hook 契約。** Claude Code 與 Codex 轉接器共用 vault 清單、事件名、預算、trigger 欄位與核心工具。這是兩個已支援 host 的具體契約，不是「所有 CLI、未來每次升級都已保證不壞」的寬口徑。

## 目前賽道的失敗模式

下表只談病，不點名產品。右欄每個命令都只用本 repo 的合成資料自驗。

| 失敗模式 | Epitype 的對治 | 自己跑證據 |
|---|---|---|
| 喚回靠 AI 自覺，或只在 session 開頭做一次 | prompt 情境檢索，加上逐工具 trigger 比對 | `python adapters/claude/recall_hook.py --selftest`<br>`python adapters/claude/pretooluse_gate.py --selftest` |
| 寫入端知道決策已失效，讀取端仍可能端出舊版 | 結構化決策狀態、讀取端預設排除、保留 provenance，以及明示的考古覆寫 | `python epitype/memsearch.py --selftest`<br>`python adapters/claude/recall_hook.py --selftest`<br>`python epitype/decision_lint.py --selftest` |
| 規則存了卻攔不住動作 | 帶 trigger 的傷疤卡驅動有界 deny、替代路與稽核列 | `python adapters/claude/pretooluse_gate.py --selftest` |
| 評測只量召回，不量行為 | 元件 selftest 驗輸出、邊界、失敗模式與往返；v1.0 再加發布筆試 | `python tests/run_all.py` |
| 自動萃取把猜測與糾正變成無人負責的髒記憶 | transcript 掃描只產提案；結構化決策必須先通過 lint 才算現行 | `python install/scar_scan.py --selftest`<br>`python epitype/decision_lint.py --selftest` |
| 安裝、升級或解除安裝太脆弱，吃掉全部信任 | dry-run、標記式 merge、原生保護、合成體檢、vault 保留與可自驗往返 | `python install/graft.py --selftest` |

逐病的病象、成因、對治與驗證邊界，見 [失敗模式詳解](docs/FAILURE_MODES.md)。

## 快速開始

需求只有 Python 3，以及一份可偵測的 Claude Code 或 Codex 設定。Epitype 僅使用 Python 標準函式庫。

在 repo 根目錄先預覽，不落任何變更：

```powershell
python install/graft.py install --dry-run
```

確認輸出只涵蓋你要改的 host 與位置後，再安裝並跑合成體檢：

```powershell
python install/graft.py install
python install/graft.py doctor
```

安裝器會 merge 帶 Epitype 標記的項目，既有 host 檔有變更前會先備份，並把共用設定指向偵測到的原生 vault。若沒有原生 vault，才建立空的 fallback vault；它不會停用原生記憶。重新安裝會保留既有的 vault 策展清單；若要改採目前偵測結果，必須明確執行 `python install/graft.py vaults --resync`，並可先加 `--dry-run` 預覽。

若 `doctor` 顯示 `SHIM FAIL-OPEN SEEN`，先檢查並修復列出的原因，再用 `python install/graft.py doctor --clear-shim-status` 明確確認並清除該次無聲下線紀錄。

`doctor` 與 SessionStart 會從 `_EPITYPE_FINDINGS.md` 浮出仍開啟的記憶路徑 finding；先用 `python install/graft.py findings ack <code>` 確認，再於修復後用 `python install/graft.py findings close <code>` 關閉。

repo 搬家時只需執行 `python install/graft.py relocate --to C:\新的\repo\路徑`，不必修改 host 的 hook 設定。

可從 [`templates/`](templates/) 選一個起點：`minimal` 適合單人單機，`team` 是共用 vault 加統一寫鎖契約，`power` 則保留全件、census 與 exam-ready 目錄。

## 本機搜尋索引

先明確建立 vault 的本機 FTS 索引，再執行查詢或情境喚回：

```powershell
python epitype/memsearch.py build C:\path\to\vault
python epitype/memsearch.py query 關鍵詞 --vault C:\path\to\vault
python epitype/memsearch.py recall "自然語言提示" --vault C:\path\to\vault
```

產生的資料庫位於 `<vault>/.epitype/memory_fts.sqlite3`，Git 會忽略它。`query` 與 `recall` 不會建立缺少的索引：此時會以非零退出碼回傳 JSON `no-index` 錯誤並指引先跑 `build`；已有有效索引但沒有命中時，仍維持原本的零結果 payload。既有索引若已過期，仍會做增量更新，該次回覆會帶 `index_updated: true`。`build` 也會在現行目錄尚不存在時，把偵測到的舊版索引原地搬到現行目錄沿用；若新舊目錄同時存在，Epitype 會採用現行索引，並提示舊目錄可在驗證後刪除。

## 解除安裝

一樣先預覽，再只移除 Epitype 自己擁有的註冊與設定：

```powershell
python install/graft.py uninstall --dry-run
python install/graft.py uninstall
```

原生記憶設定與 vault 卡片都會保留。對「安裝後沒有再被修改」的 host 註冊檔，合成往返測試是 byte 級一致；若後續已有其他變更，解除安裝器只拿掉帶標記的 Epitype 項目，不會抹掉新內容。手動還原備份前，先讀 [解除安裝說明](docs/UNINSTALL.md)。

## 架構

Epitype 把穩定習慣、事故傷疤與待辦分開；提供四條檢索路；並替決策與傷疤建立明確生命週期。詳見 [架構說明](docs/ARCHITECTURE.md)。

## 版本政策

- **v1.0** 代表通過筆試發布閘，不只是打一個 tag。隨箱行為筆試未過，就不發布。
- v1.0 之後按月吸收真實使用所得；新規則或新攔截必須有事故／觀測依據與可重跑驗法，不能只因看起來新穎就加入。

## 限制

1. 需要安裝的方案，不可能比「原生預設已開」更省事。Epitype 的價值是治理，不是零設定。
2. hook 注入受 host 的輸出量與時間上限約束。Epitype 把輸出封頂在 10 KiB，並採三秒 fail-open；所以必須選內容，不能全塞。
3. 行為層量測仍在早期。現有 selftest 使用合成資料。筆試引擎已隨箱（`exam/` 內含小型合成示例題庫）；尚未完成的是整份發布筆試的實跑與通過——那正是 v1.0 的門檻。
4. 已交付的現行決定保證涵蓋 Epitype 預設的 `query`、`recall` 路徑，以及沿用預設的 hook 消費端。直接讀檔不在這層過濾範圍；`--include-superseded` 則會為考古刻意顯示保留的歷史卡。
