# GitHub 專案評估

查核日期：2026-11-08。評估目的是改善 Epitype 現有的本機記憶搜尋、驗證與文件。
採用概念、使用開發工具與替換產品後端是不同的變更；下表列明實際採用狀態。

| 專案 | 對 Epitype 的幫助 | 本輪選擇與成本 |
| --- | --- | --- |
| [tobi/qmd](https://github.com/tobi/qmd) | [精確片語與混合搜尋](https://github.com/tobi/qmd/blob/main/docs/SYNTAX.md)，以及依 collection／metadata 限定搜尋範圍 | 已借鑑完整原話比對；專案隔離也採先確認範圍再送出內容的做法，沿用 Epitype 自己的 SQLite／stdlib 實作。完整 QMD 需要 Node 22+，並使用原生套件及模型相關依賴；本輪未安裝或替換後端 |
| [thedotmack/claude-mem](https://github.com/thedotmack/claude-mem) | 依專案保存對話記憶，先提供索引、再按需開啟完整內容 | README 的分層取用方式值得參考；Epitype 已有卡片、來源及送達入口，本次補上逐卡範圍檢查。未安裝另一套攔截器、服務或額外記憶系統，避免重複注入；查核時 LICENSE 為 Apache-2.0。未採用其宣傳的省量比例作為 Epitype 證據 |
| [evildmp/diataxis-documentation-framework](https://github.com/evildmp/diataxis-documentation-framework) | 按讀者要學習、操作、查規格或理解原因來組織文件 | 已用於 [文件入口](INDEX.md) 的導覽安排；另列日期明確的實驗紀錄。框架文件為 CC-BY-SA 4.0；本輪連結與引用概念，未複製其正文 |
| [lycheeverse/lychee](https://github.com/lycheeverse/lychee) | 檢查 Markdown 檔案連結及標題錨點，避免整理後留下斷鏈 | 使用校驗碼固定的 Windows CLI 作離線文件檢查；開發工具，不加入 Epitype 執行依賴。MIT／Apache-2.0 雙授權 |
| [mkdocs/mkdocs](https://github.com/mkdocs/mkdocs) | 從 Markdown 產生可瀏覽的靜態文件網站 | 現有 GitHub／本機 Markdown 已能提供入口，暫不增加網站建置與發布流程。若要公開文件網站，再評估；BSD-2-Clause |
| [HypothesisWorks/hypothesis](https://github.com/HypothesisWorks/hypothesis) | 用連續新增、移動、重試等操作找出索引狀態錯誤 | 已放在選用的測試依賴中，驗證歸檔與快取序列；不在產品執行期載入。MPL-2.0 |
| [executablebooks/markdown-it-py](https://github.com/executablebooks/markdown-it-py) | 獨立解析 Markdown 結構，核對歸檔標題與清單脈絡 | 已作選用測試的獨立比對工具；MIT；沒有增加產品執行依賴 |
| [ccusage/ccusage](https://github.com/ccusage/ccusage) | 讀本機 Claude／Codex 用量紀錄 | 本輪已用它交叉核對可取得的用量。紀錄缺漏、快取命中與壓縮 token 都不能直接當成 Epitype 省費；MIT |
| [sirmalloc/ccstatusline](https://github.com/sirmalloc/ccstatusline) | 顯示 Claude 提供的狀態列用量、context 與快取資訊 | 已閱讀原始 README；可作持續觀察的介面，但不能補出宿主沒有提供的逐來源載入量。本輪沿用 Epitype 現有報告，未安裝另一個狀態列或新增服務 |

本輪採用的原則是：先修可重現的現有錯誤，再用同一份資料比較效果。
語意搜尋、額外模型及公開文件網站屬於更大的產品選擇，這份評估不代表已安裝或啟用。

公開的變更與限制見 [1.11.0 發版說明](releases/v1.11.0.md)。
