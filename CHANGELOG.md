# Changelog

## Unreleased

- 自動捕捉改折衷制（owner 2026-09-09 裁 Q5「C」；FAILURE_MODES §32）：只有形狀明確的三個模板自動入庫——`arrow-answer`（有 `<-`／`<=`／`《` 回覆標記，且 owner 那半以短答開頭：同意／可／不／好／甲乙丙／A–E／yes／no）、`leading-correction`（owner 那半**句首**是不是！／不對，／不要／別再／錯了／stop）、`explicit-grant`（明示第一人稱授權：我同意／同意過／我授權／准你／批准你／允許你／你可以＋動詞／I agree／you may）。判定只看 owner 自己那半（`capture.owner_side`：裁定卡正文帶著助理的提問，不能讓助理替 owner 蓋章），常數在 `memspec.CAPTURE_ADMIT_*`。
- 其餘「現行規則仍會捕捉」的句子改寫提案：`<vault>/_drafts/captured_pending/YYYYMMDD/<原本的檔名>.md`。`_` 開頭的路徑段本來就不在 `memsearch._scan_vault` 的掃描範圍，所以提案不進索引、不被喚回、也不受寫檔閘的卡片契約管——沒有第二條排除規則要同步。線上 hook 與離線回放（harvest）共用 `capture.write_capture` 這一處判定，落點一致；`--dry-run` 多印一種 `WOULD PROPOSE`。
- 轉正是人的動作，不是回放：`harvest --reevaluate --apply` 對 `verified: false` 的提案印 **HOLD** 不搬（提案本來就是「今天的規則也會捕捉」，那正是它被扣住的原因），dream 第 4 節改列待審份數與人工審閱指令，不再對捕捉提案提供 `--reevaluate` 那條命令。
- 每張自動寫的卡（入庫與提案皆同）frontmatter 帶 `provenance: auto-captured` 與 `verified: false`；轉正時改 `verified: true` 並補 `verified_by`／`verified_at`。四個欄位列入事件卡型別的選填欄，`epitype cards` 不因此 WARN。
- 消費端逐處查證後釘進回歸：Stop 決策閘（`decision_key`＋`status: active`）、寫檔閘規則 A（同一批決策卡）與規則 B（`_` 路徑段不算卡）、PreToolUse 授權判定（只讀宣告 `trigger:` 的卡）、SessionStart 現行裁定清單（同決策卡門檻）都吃不到 `verified: false` 的卡；喚回照舊給它 pinned 席位但只掛「歷史捕捉（非完整對話／現行裁定）」前綴，不掛決策前綴。
- 精準度（`tests/capture_precision.py --local`，254 句人工標記；`exam/exam_runner.py` 跑不了這份題庫——它要 `questions` 清單）：判定那條不變（精準 0.807、召回 0.850）；自動入庫那條精準 0.750、召回 0.240、value 0.600，自動入庫卡由 119 張降為 40 張、判錯的由 23 張降為 10 張、不值得留的由 34 張降為 16 張，79 句改成提案。**比例沒有變好**——同一份標記上被扣住的那堆反而比入庫的那堆漂亮（0.835／0.772 對 0.750／0.600）；這次換到的是「未經人核的材料少了三分之二不再自動進喚回」，不是更乾淨的比例。owner 已接受召回下降。
- 回歸：新增 `tests/capture_admission_regression.py`（8 案，含四道閘的逐處查證）納入 run_all（48/48 → **49/49**）；`epitype/harvest.py --selftest` 20→23、`adapters/claude/recall_hook.py --selftest` 45→46、`tests/capture_integration_regression.py` 每個題目多驗落點與兩個新欄位。

- 移除四項行為層功能（owner 2026-09-09 逐題裁定「A」；FAILURE_MODES §30）：生成前守則（QUESTION_PREFLIGHT＋TURN_CONTINUITY＋每場一次的 guide 標記）、操控收尾規則（control_lifecycle）、AI 承諾帳本（commitments：Stop 抽句、SessionStart／PreCompact 提醒、dream 第 4 節）、旁白計量（narration_meter）。SessionStart 與 UserPromptSubmit 不再輸出任何守則文字，PreToolUse 不再附操控指引也不再出「⛔ 旁白」，Stop 不再寫 commitments.jsonl。根因：AI 把每條糾正反射成產品機制、沒算機制本身的 token 成本。
- v1.2.0 已出貨的承諾帳本（`epitype commitments`）與旁白量測（`epitype narration`）兩個子命令在下一版屬【移除】：兩支 CLI、兩個模組、相關 hook 呼叫全部不再存在。各庫既有的 `.epitype/commitments.jsonl` 不刪、不搬，只是沒有任何路徑再讀它。
- 退役測試三支（功能依裁定移除，題目隨之失效）：`tests/control_lifecycle_regression.py`、`tests/commitment_persistence_regression.py`、`tests/question_premise_regression.py`（後者的兩項反面守衛——提問工具不得被擋、收尾句不得被 Stop 擋——改以 `tests/no_semantic_gate_regression.py` 延續）。文件退役：`docs/QUESTION_PREMISE_VALIDATION.md`、`docs/TASK_CONTINUITY_VALIDATION.md`。**考題退役：0 題**——六份題庫全掃過，沒有任何題目依賴被移除的功能，分母不變：corpus 330/330、seeds 15/15 與 5/5。
- 閘門：run_all 由 52/52 降為 **48/48**（兩支模組 selftest＋三支回歸退役、一支新增）、privacy PASS、corpus 330/330、seeds 15/15 與 5/5、doctor HEALTH PASS 5/5。各 hook selftest 分母更新：pretooluse 73/73、stop 22/22、sessionstart 31/31、precompact 8/8、recall 45/45、dream 31/31。

- 記憶目錄改生成式（owner 2026-09-09 裁 Q8 丙；FAILURE_MODES §31）：新增 `epitype views`，依卡片欄位生成 `_views/current.md`（現用卡＋**全部**現行決策）與 `_views/history/closed.md`（已結案／已取代）。掃描範圍與 memsearch 同一份、型別判定與 card_lint 同一份，不再造第二套掃描器；生成器永遠不寫 `MEMORY.md`（它有多個併發寫者），輸入指紋沒變就不重寫，同庫並行共用鎖。`epitype dream` 順路重生。
- 專案卡收得下 `status: closed`（另可寫 `closed_at`／`closed_by`／`closed_evidence`），決策卡仍只有 `active`／`superseded`；寫錯型別的 status 是 FAIL。**`closed` 只改目錄位置，喚回照舊搜得到**，只有 `superseded` 會轉向繼任卡——memsearch 加一項回歸釘住這句話。
- `epitype cards --deep` 加庫層級檢查：決策唯一性與取代鏈折用既有的 `decision_lint`（不另寫一套），再加兩項新的——納管卡是否漏出生成目錄、是否漏出搜尋索引，各自帶重生指令。SessionStart 那一行走的仍是不含庫層級檢查的淺掃描，開場成本不變。
- SessionStart 對非原生載入索引的宿主（Codex）改回音**完整**短入口：按真實 JSON 編碼位元組預算，裝得下就整段，裝不下才排序取樣並在最後一行明說「送出 N／全文 M bytes」與正本路徑；不再固定截前 3 KB 而不留痕跡。Claude 端維持不回音。
- 回歸：`tests/views_regression.py`（11 案）、`epitype/views.py --selftest`（12 案）納入 run_all；card_lint 41/41、memsearch 50/50、sessionstart 30/30、dream 31/31。

- SessionStart 在 Claude Code 上不再回音 cwd 的 MEMORY.md（宿主本來就會載入，回音重複 ≤3 KB 並擠掉帳本）；依 transcript_path 位於 `.claude` 判定宿主，只跳過該 cwd slug 的庫，Codex 與治理庫照舊（owner 2026-09-09；FAILURE_MODES §29）
- 生成前程序（提問前查證＋任務續行，共 2,427 字元）改為每場一次：SessionStart 或第一個 prompt 送出後，同場後續 prompt 不再重送；壓縮清掉標記後補送；無 session id 維持每 prompt（owner 2026-09-09 選項 B；FAILURE_MODES §28）

- 操控工具階段補完整收尾程序：必要才開、記錄自建視窗／分頁／分組、用完即關、另結束操控，依工具結果確認並保留原有資源。沿用現有 PreToolUse，既有拒絕優先，無新增權限、常駐監控或模型；每次匹配增加696B，普通讀檔／shell不增加。7項回歸、安裝shim與隔離還原通過；原生Codex送達，以及Claude App原生Chrome分頁／分組建立後關閉已有工具證據。這是工具階段指引，不是自動關閉或每次遵從的保證；測試偏差與覆蓋界線見 FAILURE_MODES §26。

- 安裝自測複製專案時排除並行 trust 測試自建的暫存目錄，避免複製期間目錄消失導致失敗；新增排除邊界斷言，自測41/41，正式安裝邏輯不變。完整回歸與補測的分段證據見 FAILURE_MODES §27。

- 自動捕捉的 description 不再默默裁到 80 字；喚回改讀命中原卡正文，附來源與歷史身分，保留尾端限制。超長內容明示未完，讀不到原卡不拿索引摘要冒充全文；現行決策優先、去重與輸出預算不變。新增 7 項回歸，完整 49/49，無新增運行模型呼叫；另經原生 Claude App 一般文字入口確認來源送達、歷史與永久授權分辨正確，不沖掉其他既有語意失敗。

- 補正 Git 傷疤範例的完整指令／操作邊界，避免 `--no-checkout` 及診斷正則誤擋；保留真正破壞指令、包裝命令及解析失敗時的保守檢查。新增 31 項合成 gate 回歸、完整 48/48；既有本機卡需明確更新，範例不會自動覆寫使用者規則。這不代表原生提問語意缺口已修好。

- 修復壓縮復原的來源身分：保留人類排隊訊息、排除宿主摘要、助理引文對回原始行、訊息截斷明示；Codex response-item 與 scar 候選掃描共用解碼。新增唯讀 `source` 指令及地圖回原文路由，顯示搜尋範圍、漏項、版本與雜湊，不把找到原文冒稱前提成立。來源／查找各 9 項回歸，完整 47/47；零新增運行模型呼叫，舊原生語意 FAIL 不因此取消。
- 修後原生 Claude App 已實讀原文／報告／設定、保留正常提問與兩階段續行；但仍將未測推成未實作，整體語意驗收 FAIL。另實際執行新查找器，並修正操作者提示封裝以免合成訊息自動入裁定庫；這是驗收隔離，不是新語意硬閘。

- Stop 新鮮度回歸改用每個測試自己的暫存標記目錄；固定 session 名不再撞到宿主先前留下的防重標記。連跑兩次各 9/9，完整套件 45/45；未改運行期閘門、未清除宿主標記。
- 原生 App 再驗「實作／驗證分軸」提示仍有語意失敗，已撤回本輪無效增量並保留對照；既有候選不冒稱通過。補查找範圍、指標與來源版本的合成覆核案例，無額外運行模型成本。

- 提問程序補查選項內文、開發缺口與效果依據；未測不等於未做，建議不取代使用者決定。新增雙入口送達回歸，共十二項；程序本輪增加 392B，提問 1,372B、連同續行共 2,427B。零額外運行模型呼叫，原預算及權限不變。
- 原生 App 實測確認生成前送達、真實讀取、文字／工具提問及兩階段續行；但仍有自行補出能力與實作狀態的失敗，**此候選尚未通過整體行為驗收**，不是語意硬攔截。補公開合成反例及操作者代答規程；私人對話與量測另存，不發布。

- 修復十二項安全盤點問題：收割同路徑刪卡、隔離與歸戶覆蓋、別名換行注入與過期快照寫回、搬家後排程失聯、crontab 讀取失敗誤當空表、即時／回放分類不同步、Dream 不完整結果誤報乾淨與鎖接管競態、承諾未落盤卻報成功，以及決策快取沿用舊權限。保留卡片、既有成功回報與 hook 失敗開放契約；新增十一組合成回歸納入完整測試。

- 加入同源任務續行程序：插問、確認、相關 bug 回報不清掉原任務與有效授權；收尾前比對整體交付，保留真正待答、分析限定及明確叫停。由既有訊息／開場 hook 送達，不新增 Stop 硬擋、原生 goal 解析或每回合模型呼叫。
- 續行程序首版另增 1,055 UTF-8 bytes，當時連同提問程序共 2,035 bytes；預算不足先完整保留原提問程序。當時共享回歸增至十一項，補正常停點與原生 App 分階段驗收協定；不宣稱通用語意判斷已被機械化。

- 在現有 UserPromptSubmit 與 SessionStart（開場／恢復／壓縮後）路徑加入共用的提問前查證程序；不依賴使用者問句命中記憶卡。區分已驗能力、具體開發途徑與待驗範圍，保留偏好、授權及假設設計。
- 提問程序首版與喚回共用輸出上限；當時每次送達增加 980 UTF-8 bytes，無額外模型呼叫。首版新增八項離線回歸與合成驗收流程；這是生成前指引，不是任意前提的語意硬攔截，也不代表 App 全面驗收或改善幅度已證明。

- 喚回先排除已送出的卡再分配八筆名額；不擴大每庫候選上限。
- 跨庫一般卡採可比較的查詢命中詞數與庫內排名合併，保留單庫順序及裁定優先；過長的一般卡可讓位給完整短卡，不跳過尚未容納的裁定。輸出與時間上限不變。
- 新增跨庫、去重補位、整卡預算及權限順序的離線回歸；不需要模型呼叫。

- 修正中文標點黏住英文關鍵字造成漏搜；保留技術名稱內的 ASCII 標點，新增檢索回歸。
- Codex 信任檢查改查原生 `hooks/list` 與目前雜湊；舊快取不得代替原生核准，無法查證時不報通過。不會啟動模型或代替使用者核准。

- 治理邊界修正：重問攔截僅採用附原話的 `owner-explicit` 裁定；保留決策來源並更新快取版本。
- 喚回不再把索引中的舊裁定降級成一般資料送出；原卡已作廢或不可讀時剔除。
- 喚回逐卡在輸出並 flush 成功後才標記，逾時、輸出失敗及預算未容納的卡保留重試機會。
- 設定中單一庫失效不再中止其他庫喚回；hook 回報降級，暫停歸屬不明的自動寫入。
  新增 `tests/governance_regression.py` 合成回歸，納入 `tests/run_all.py`。

- U65 捕捉落點：專案的進專案庫。2026-09-06 稽核實證：自動捕捉的 owner 事件卡
  （`grants/ corrections/ rulings/`）一律落治理庫，所以在專案對話裡講、話裡明講該專案的
  裁定與糾正被寫進通用庫——治理庫 132 張事件卡裡 74 張的 `cwd` 指向另一個已登記的專案庫。
  規則改成依「這場對話屬於哪個專案」落點：`cwd` 或其任一層祖先對應到已登記的原生記憶庫
  （已有索引或卡）就落最相關的那一個，都沒有才落治理庫；宿主開的空殼不算庫（卡退回治理庫
  但仍記 `cwd`）。落點規則集中在新的 `epitype/capture_route.py`，線上 hook
  （`_hook_common.capture_vault`，原生庫解析也搬到同一處）與離線回放（`harvest`）共用一份；
  回放另補 Codex 的 `cwd`（只在開場 `session_meta` 出現一次，實測 40 張卡因此沒有來源專案），
  並為每個寫過的庫各自重建索引、同句話在治理庫已有卡就不再於專案庫長第二張。既有誤置卡不由
  捕捉端搬：`epitype capture-route <vault> --audit` 唯讀列出 `MISROUTED <卡> -> <庫>` 與統計，
  `--apply` 才搬（`os.replace`、同名加 `-2`、永不刪，並在正文補一行歸戶註記）。跨專案通用的
  長效規則仍該進治理庫，但那是人立卡的判斷，自動捕捉不猜。合成測試的家目錄一併隔離
  （`run_synthetic` 預設把 HOME 指到暫存路徑）：真機上 `C:\` 是每個暫存 cwd 的祖先且它的原生庫
  就是治理庫，沒有這道隔離，一次 selftest 就會把卡寫進真庫。`capture_route --selftest` 11、
  `recall_hook --selftest` 44→45、`harvest --selftest` 18→20。詳見 FAILURE_MODES.md §17。

- U64 引用不算再提議：2026-09-06 owner 回報閘門實測表時引用一句已判 `forbidden` 的話
  當測試案例的證據，被 Stop 閘當成又端回去而擋下整份報告；同一天為同一條 `forbidden`
  寫驗證腳本，腳本裡的禁詞字串常值也被寫檔閘規則 A 擋下。規則：`forbidden` 命中若整段
  落在引用區段內（中文引號「」『』、直角＋彎雙引號、彎單引號、單行反引號／```圍籬```、
  Markdown 引用行 `>`）不算再提議；區段外仍有命中——含同訊息引用一次、另一處裸提
  一次——照擋。兩道閘共用 `stop_gate._forbidden_fragment`，改一處兩邊都好；規則 A
  「只豁免定義該裁定的卡本身」（U63）未動。ASCII 直引號沿用既有排除
  （2026-09-03 對抗審查 #5：英文縮寫會配假引號區間）。`stop_gate --selftest` 18→23、
  `pretooluse_gate --selftest` 73→75。詳見 FAILURE_MODES.md §16。

## v1.2.0 (2026-09-06)

The hot-path release. Measured on a machine at 100% CPU with two real vaults
(158 + 294 cards): a recall or gate call that took 4.5–5.5 s — past both the
host's and its own 3 s deadline, so it silently injected nothing and let a
destructive command through — now takes 1.2–1.7 s end to end, and the work
inside the interpreter fell from 4.0–4.6 s to about 0.4 s.

### Fixes that mattered in the wild

- The vault scan reads directory entries and never resolves a path: symlinks and
  Windows junctions are recognised from the entry's own attributes, and `_`- or
  `.`-prefixed parts are never entered. Inside its grace window the stale check
  reads nothing; after it, the stored path/mtime/size manifest is compared, so
  deletions, renames, backdated additions, and size changes cannot stay hidden.
  Results carry `card_path`, so no caller resolves the vault again. The selftest
  lists 300 cards and asserts that no path is resolved.
- The action gate finds trigger cards through a manifest cache
  (`<vault>/.epitype/gate_triggers.json`) and re-reads only cards that changed.
  Its regex validator rejects only shapes that can backtrack exponentially
  (nested unbounded repetition, alternation under repetition, backreferences);
  adjacent repetitions and bounded groups are accepted and the length limit is
  1024. A card the gate cannot use is named to the model once per session rather
  than skipped silently. A matched rule stays a deny when audit logging is
  contended or the advice exceeds the output ceiling.
- Recall lines say each fact once: a name the path already spells and the
  "owner … auto-captured" label are dropped (about 40% fewer bytes per pinned
  line). The advisory and vault legend are sent once per session per distinct
  legend; compaction clears the session's recall markers, so they — and any
  correction or ruling injected before it — return afterwards. A capture that
  cannot take the index lock ages the index so the next prompt rebuilds it.
  Every injected block that a budget cuts ends with the count of pieces left
  out instead of silently skipping middle pieces.
- One frontmatter reading for the index, the lints, and the gate
  (`memspec.split_frontmatter`, `parse_scalar`, `join_block_scalar`): the BOM,
  CRLF, `...` document ends, duplicated keys (first wins), `|-`/`>-` block
  scalars, and a quoted `#` now read the same everywhere;
  `tests/frontmatter_consistency.py` proves it card by card.
- Decision lint: a key whose every card is retired warns rather than fails,
  keeping the exam contract; supersession chains are valid unless they loop; a
  replacement must share the decision key.
- Installer: doctor accepts a `python` on PATH or any existing interpreter in a
  registration instead of the exact path of the interpreter running doctor;
  host-file backups keep only the newest three; `hook_trust` prints
  `UNVERIFIED` (exit 1) instead of a `SKIP` that read as green; `python -m
  epitype` gains a `__main__` guard; a tag publish requires `package.json` to
  carry the tag version; `tests/run_all.py --jobs N` runs selftests in parallel
  on CI.
- Recall terms drop particle bigrams and punctuation fragments (「我們」,「的虛」,
  「——」), which matched every card and pushed the rule card that answered the
  prompt below the window; a ruling matched only in its body (the assistant's
  question, not the owner's answer) is an ordinary hit rather than a pinned one.
- Hook commands are registered without quotes whenever the paths allow it. Codex
  on Windows runs hooks through `cmd.exe /C`, which strips the first and last
  quote of a line that starts with one, so the fully quoted form the
  2026-09-04 batch introduced failed every Codex hook before Python started. A
  path that needs quoting also gets a `commandWindows` form wrapped in one outer
  pair of quotes. Doctor prints each hook's wall time and warns when
  `repo_root` has uncommitted changes.
- Hook registrations allow ten seconds instead of three (owner ruling
  2026-09-05); the hook's own deadline is nine. Existing installs pick this up
  by re-running `install`, and Codex hosts then need the five hooks trusted
  again because their definition changed.
- From the 2026-09-04 batch, kept as found: incremental index builds use mtime
  and size; owner captures and compact maps share the governance vault; compact
  maps are per session and bounded; the installed package provides a unified
  `epitype` command and CI exercises the installed wheel; Codex trust requires
  exactly the five supported registrations.
- SessionStart 開場段預算——2026-09-06 Codex 端首場 SessionStart 被宿主記成 Failed：
  `expired()` 只在段與段之間被檢查，段內無界的全庫掃描（當時是待辦摘要）能單獨吃光
  10 s 才被砍，整場注入一起消失。改為每段軟預算：待辦摘要與型別摘要各 1 s、開場總
  預算 5 s，超時整段省略而非整場失敗；`_dream_mark_notified` 狀態檔改寫暫存檔再
  `os.replace`，砍在中途不留 0 byte。FAILURE_MODES §9。
- 三個 token 洞（2026-09-06 真機量測，FAILURE_MODES §15）。**泛詞查詢**：一句與記憶
  無關的「今天天氣如何」原本注入 2007 bytes／7 張卡，命中的全是「今天」「天天」碰到
  卡片正文。查詢端把泛詞排除在「命中」之外（`memspec.RECALL_GENERIC_TERMS`：時間詞、
  量詞、填充詞、英文虛詞；裸數字不再入切詞；「一成較」入虛詞字表），沒有任何實詞命中
  就整份不注入；單一中文二元組只認卡的身分欄（name／aliases），碰到描述或本文要第二個
  實詞背書——跨詞界的碎片（「馬拉松|前一天」切出「松前」「前一」）跟真詞一樣多。庫內
  高頻詞刻意不當泛詞：實測 bug 佔該庫 30%、titan 41%、記憶 25%，用 df 比例判泛詞會殺掉
  答案卡。「今天天氣如何」2007→0 bytes、「幫我翻譯這句英文」1880→771、「titan 回測為
  什麼變慢」1815→1653；真庫 56 題回歸 48/56、合成 51 題 45/51 兩者皆不變。**開場裁定
  清單**：12 條各帶完整 owner 原話＝1977 bytes，原話在喚回命中那張卡時本來就會送。開場
  改成一條只列 `decision_key｜日期`，並只列近 30 天（`SESSIONSTART_DECISION_RECENT_DAYS`）
  或帶 `forbidden`（會擋人、不受上限擠掉）的那些，其餘一行收尾說還有幾條；兩庫合計
  2167→576 bytes（開場總量本來就頂到 `budget_bytes`，省下的位元組換成原本被丟掉的
  帳本／索引段落：掉段 42→35、行數 61→103）。開場預算 10240→8192
  （`memspec.HOOK_DEFAULT_BUDGET_BYTES`）：`sessionstart_hook --selftest` 28/28、
  `exam_runner` morning_review 15/15、corrections 5/5、corpus_300 330/330 全過。
  **承諾誤抓**：真庫 23 條 open 有 6 條是
  過程旁白。承諾只從訊息結尾那一段抽（`COMMITMENT_TAIL_CHARS`，過程段不算）、執行旁白
  詞不算承諾（`COMMITMENT_NOISE_PATTERN`：Private list／verifier／background／shell／
  pytest／正在跑／跑完…）、一回合最多 2 條（原 5）、open 超過 7 天
  （`COMMITMENT_STALE_DAYS`）自動標 `expired` 並不再計數；開場那行改印最多 3 條摘要
  （各 ≤60 字）。新增 `commitments.py <vault> --requalify --dry-run` 用現行規則重評既有
  帳本並逐條印 keep/drop（只印不套用），以及 `--expire-stale`。
- U59 泛詞洞的英文半邊：2026-09-06 真機實測「how do I tie a bow tie」注入 2073
  bytes／27 張候選卡，全是 `tie` 撞進 `tier`／`tiered`／`service_tier` 的字首巧合；
  英文詞 ≤3 字母改認詞尾邊界（`term + \b`，`memsearch._short_latin_pattern`），
  `bug` 仍吃得到 `debug` 的字尾（回歸題庫 titan-log-over-screenshot 要的真命中），
  `tie` 吃不到 `tier` 的字首，同一批詞的單一實詞命中也比照中文二元組加身分欄門檻；
  該句真機注入歸零，真庫 56 題回歸 48/56、合成 51 題 45/51、`memsearch --selftest`
  43→47（新增 4 題）皆不退。`capital` 撞見同庫高頻多義詞留在原地未修：試過把身分欄
  門檻推廣到全體英文實詞，`memsearch --selftest` 的 body-only 真命中案例直接斷言
  失敗（獨特詞只在 body 出現也該找得到，跟 capital 撞高頻詞是两回事，字數分不出
  來），診斷視窗仍有 ~24 張候選，但真機 `FTS_TOP_K=5` 頂帽本來就把實際注入壓到 5
  張（詳見 FAILURE_MODES §15 Known limits）。
- U62 治理日誌灌量：2026-09-06 真機實測，同一批壞掉的傷疤卡（那批已在 U60 前修好，
  是舊卡片內容遺留）讓 `_GATE_LOG.jsonl` 三天內灌到 13,061 列，其中 12,784 列是同一張
  卡每次 PreToolUse 都重寫一次的 `parse_defect`。`parse_defect` 現在每 (卡, 日) 只記一次
  （`<vault>/.epitype/gate_parse_defect_seen.json` manifest，`_append_parse_defect`）；
  三個 gate 日誌寫入點（動作閘的 `_append_audit`／`_append_parse_defect`／
  `_append_write_block`、Stop 閘的 `_audit`）統一從 stdin 事件帶 `session_id`，沒有就省略
  這欄，`gates_report.py` 既有的 `--by session` 與同 session 同卡連擋 ≥3 疑似誤擋清單因此
  可用；`_GATE_LOG.jsonl` 超過 `_GATE_LOG_MAX_BYTES`（2 MiB）時整份改名成
  `_GATE_LOG.jsonl.1`（保留一份，不刪，不接力鏈）。`pretooluse_gate --selftest` 69→72、
  `stop_gate --selftest` 16→17。

### New

- The owner-quote capture core moves into `epitype/capture.py`, and
  `epitype/harvest.py` adds an inventory-plus-replay harvest whose manifest
  skips files that have not changed. `epitype/card_lint.py` checks
  required/optional frontmatter fields per card type across 11 types and
  prints a one-line SessionStart summary.
- Recall now places a matching current decision card ahead of corrections and
  rulings, carrying the owner's own words, and SessionStart injects a
  "current rulings" block per vault (compaction-aware). Flat frontmatter-field
  reading converges into `memspec.frontmatter_fields`, shared by
  `decision_lint`, `card_lint`, and all three hooks; the hot hook path no
  longer loads `decision_lint` or `argparse`.
- A Stop decision gate (5th hook event, Claude and Codex) compares the turn
  against the owner's current decision cards before it ends: a forbidden-card
  hit, or a question repeating the same card's alias twice or more, blocks
  and asks for a rewrite per the ruling. `stop_hook_active` is never blocked,
  each card blocks at most once per session, and a block is logged to
  `_GATE_LOG` with `kind=stop_block`.
- A 72-card, 51-question synthetic recall regression exam
  (`tests/recall_regression.py`, threshold 0.882) runs alongside questions
  against the real local vault. An offline alias batch
  (`epitype/alias_batch.py`) exports cards missing aliases and applies
  reviewed aliases back — additions only, NFKC-deduplicated, preserving
  BOM/CRLF, and a collision with a decision card blocks only that alias.
- The exam runner gains `stop` (runs the real stop gate), `abstention`
  (`top_k_empty`), and recall's `pinned_contains` kinds; the sample corpus
  grows from 12 to 16 questions, and the local 330-question corpus (kept out
  of the repo) passes in full.
- PreToolUse 寫檔內容閘——Write/Edit/MultiEdit（含 Codex 對應工具）的寫入內容命中
  現行決策卡 `forbidden` 即 deny 並附裁定原話；寫進記憶庫的卡若缺該型別必填欄位即
  deny 並列出欄位＋範例；WARN 只提示；同 session 同內容只擋一次；>256 KiB 放行；
  `_GATE_LOG` kind=write_block；FAILURE_MODES §11。
- AI 承諾帳本——Stop hook 從回合結尾訊息抽「我會／稍後／下一步／I'll…」承諾句寫入
  `<治理 vault>/.epitype/commitments.jsonl`（排除轉述、疑問、已完成；digest 去重；
  每回合 ≤5）；SessionStart／PreCompact 顯示未兌現承諾；`epitype commitments <vault>
  --list|--close|--purge-closed`；FAILURE_MODES §12。
- `epitype dream <vault>…`——不呼叫模型的離線整理審核包：缺別名卡、卡片型別 FAIL/WARN、
  殭屍待辦、AI 未兌現承諾、草稿待審、裁定鏈（superseded／缺 owner_quote／缺 forbidden）、
  事件卡老化、近 7 天新增，末尾依規則排「下一步」與指令；寫到
  `<vault>/.epitype/dream_pack_<日期>.md`（`--dry-run`／`--json`／`--since`）。
- owner 事件捕捉精準度——判定改為子句層（先切子句、去掉反問子句，其餘子句須有決定性內容）、
  `classify` 為線上／回放共用的唯一判定、一句一張卡；移除「依照你建議」「怎麼還」「你可以…嗎」
  「我錯了」「跟我說／白話」、一詞式應答與助理長段分析的誤抓；`tests/capture_precision.py`（合成句
  selftest＋本機真句量測：254 句 precision 0.44→0.81、保留率 0.85）；`harvest --reevaluate <dir>
  [--apply]` 重評隔離卡（正向：草稿放回）；加 `--quarantine-drops` 反向模式，把線上已收的
  drop 隔離到 `_drafts/captured_dropped`；兩者皆 `--apply` 門控、不刪；FAILURE_MODES §13。
- 夢的三種模式——`piggyback`（預設：SessionStart 順路起一個脫鉤的低優先權背景程序，
  距上次完成超過 `interval_hours` 才起，開場預算剩不到 `DREAM_SPAWN_RESERVE_SECONDS`
  就不起，夢晚一場勝過記憶注入掉一場）／`nightly`（交給系統排程，`graft install
  --dream nightly [--dream-at HH:MM]` 註冊 schtasks 或 crontab，避免同一天跑兩次）／
  `off`；夢跑完由下一場開場印一行（四個數字＋pack 路徑，只印一次，壓縮續場不印）。
  `source: compact` 兩邊都不做：不印那一行，也不起夢——壓縮續場不是新的一場，長回合
  壓縮幾次就會起幾支背景程序搶走這場正在用的 CPU。
- 卡片 lint 的語意修正——缺別名的 WARN 指向離線別名批次而不是只點名；授權卡沒有到期日
  改判 INFO（owner 可能就是要它永久有效，不進開場那一行的 WARN 數）；缺日期先找六處
  （欄位、正文、檔名、git 首次提交…）再判 FAIL，推得的日期是 WARN date-derived；
  `--fix-dates`（先 `--dry-run`）只補一行 `last_verified_at:`，BOM／CRLF 原樣。
- 沒中文別名的卡由 AI 自己補，不再問 owner（owner 2026-09-06 裁定「你自主翻譯就好了…
  如果與卡片不同，你就主動修正」）——`no-chinese` 由 WARN 降成 INFO；開場改派一行順手
  任務（`🈳 順手補中文別名（本場 ≤3 張）`），以 `<治理 vault>/.epitype/
  no_chinese_cursor.json` 游標每場輪替不同的卡，壓縮續場不派；開場同時說一次「喚回的卡
  若與現況不符：直接修卡（舊內容標 superseded、不刪），不問 owner」。寫檔閘規則 A 加
  自我豁免：寫入後內容帶同一個 `decision_key`，或命中片段落在該內容自己的 frontmatter
  `forbidden:` 區塊裡，都不擋——改規則本身永遠允許。`card_lint` 對裸名詞 forbidden 項
  印 WARN `forbidden-bare-term`（要寫「再提議」的句形，裸名詞會連「為什麼不採用 X」的
  說明一起擋掉；FAILURE_MODES §10）。nightly 排程在 Windows 改用旁邊的 `pythonw.exe`，
  凌晨不再閃一個主控台黑窗。
- U61 新增 `epitype gates <vault> [--since Nd|YYYY-MM-DD] [--json] [--by kind|decision|session|day]`
  （`epitype/gates_report.py`）：把 `_GATE_LOG.jsonl` 唯讀整理成擋下報告——依 kind／決策卡
  或傷疤卡／天／session 分、每類最近 3 筆、同一 session 同一卡連擋 ≥3 次的疑似誤擋清單，
  用來證明治理閘門確實擋下過動作，而不只是設定上存在。

### Measured

- 本機題庫（不在 repo）2026-09-06 量測：真庫喚回回歸 56 題 83.9%→85.7%；無關問題集 20 題
  15/20→16/20；筆試 corpus_300 題庫 300→330 題，330/330 全過；owner 事件捕捉 254 真句
  precision 0.44→0.81、保留率 0.85；無關閒聊注入 2007→0 bytes；SessionStart 開場注入預算
  10240→8192 bytes（`memspec.HOOK_DEFAULT_BUDGET_BYTES`）。Codex 端端到端已實證：靠記憶
  作答、Stop 閘對改寫實際擋下。

Upgrade: `pip install -U epitype && epitype install` → Codex users re-trust hooks in the
app (new Stop event) → `epitype doctor` shows HEALTH 5/5.

## v1.1.0 — 2026-09-03

The token-economy release. Every line Epitype injects is paid for once as output and
again on every later turn as re-read context, so this release measures that cost on real
prompts and cuts it, and it closes the gaps where an owner's own words were being lost.

### Injection cost (measured on 40 real prompts)

| | v1.0.0 | v1.1.0 |
|---|---|---|
| Recall injection per prompt | 3.6 KB / 11 lines | **2.3 KB / 8.0 lines** |
| Absolute paths as a share of it | 32% | 17% |
| Session start (non-governance project) | 10 KB, hitting the budget cap | 7.6 KB |

- Recall prints one `vaults:` legend line and `V1/relative` paths instead of repeating an
  absolute vault path on every hit; descriptions are truncated; at most two body-only hits
  per vault; the whole block is capped, with pinned corrections and rulings first (U29).
- Session start injects the working directory's own vault plus the governance vault only.
  Another project's index and ledger no longer crowd the budget (U29). **Behaviour change
  for multi-vault installs**: the governance vault is the configured vault that holds the
  working ledger, and a vault reached from the working directory is always kept. If no
  configured vault holds a ledger, every configured vault is injected as before.
- Narration between tool calls ("that failure was my path typo, rerunning with C:/...")
  costs output tokens and then re-read context on every later turn while telling the owner
  nothing. The PreToolUse gate now names any such segment in one line without touching the
  permission decision; `epitype/narration_meter.py` reports per-session and per-day totals
  for release review. A 36-hour local baseline held 9,273 such segments (U28).

### Owner's words stop getting lost

- Corrections: a sentence where the owner corrects the agent is captured verbatim into
  `<vault>/corrections/`, indexed immediately, and pinned first at recall with a visible
  marker, ahead of whatever plan card matched better lexically (U25).
- Rulings: when the previous assistant turn explicitly asks the owner to decide, the reply
  is captured with the question it answers into `<vault>/rulings/` and pinned the same way
  (U26). A bare mention of the word in a report, or a request phrase inside quotes, no
  longer counts as a request (U30, U31).
- Captured cards carry the owner's words in their description, so the injected line is
  self-contained (U29).

### Stale to-do items

- `epitype/pending_lint.py` names to-do lines that have an entry but no exit: no runnable
  `verify:`, not closed, older than the configured window. Session start announces the
  count in a single first line so the budget cannot drop it (U25).

### Fixes

- Windows CI: four legacy-index selftests failed only on GitHub's Windows runners, which
  hand `tempfile` an 8.3 short path under the runner profile that never equals the
  product's resolved output. Every selftest fixture root is resolved, and `tests/run_all.py`
  fails on any unresolved one (U27).
- `.github/workflows/ci.yml` runs on master pushes, `v*` tags, and pull requests instead of
  every push.

### Found by pre-release adversarial review

An independent review of the changes above found five defects, each reproduced before it
was fixed and now covered by a selftest:

- A correction or ruling that matched only in its body was discarded by the weak-hit cap,
  and pinned lines were exempt from the total cap, so recall could exceed its own limit.
  Card kind is now decided before any cap, the cap covers every line, and pinned cards are
  searched in a window three times the ordinary one so a correction ranked below the
  top-k still surfaces.
- The `vaults:` legend could be dropped by a tight budget while `V1/...` hits survived,
  leaving aliases nothing could resolve. The legend now shares the required first block,
  and a path that cannot be made relative is printed in full rather than given an alias.
- A working-directory vault that also appeared in the configured list was dropped from
  session start.
- A request phrase inside Markdown backticks still counted as a request for a ruling.
- Captured cards are persistent, indexed, and re-injected later, so credential-shaped
  text (`token=`, `sk_...`, private-key headers, JWTs, `user:pass@host`) is now refused at
  capture time instead of being copied into the vault.
- Narration markers in the temp directory are swept after a day instead of accumulating.

## v1.0.0 — 2026-09-02

First public release: local recall over Markdown memory cards (SQLite FTS5), a card-driven
PreToolUse safety gate, a mechanical compaction map, decision-card lint, working-ledger
budget gate, owner-grant capture, and the `epitype-graft` installer for Claude Code and
Codex CLI.
