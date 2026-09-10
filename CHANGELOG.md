# Changelog

## Unreleased
- tests：`source_lookup_regression` 的「掃描期間檔案變了」案例改用**讀檔前後翻面**，不再假設 `Path.stat` 只被呼叫兩次。Python 3.11 的 `Path.resolve()` 自己也會呼叫 `Path.stat`（3.13 不會），序列式 `side_effect` 因此多吃一格 StopIteration，v1.3.0 的 CI 在 3.11 兩個 runner 上 49/50。產品行為未變（3.11 實跑 lookup 正常），這是題目對 pathlib 內部細節的錯誤假設。**通案教訓**：本機閘門只跑 3.13，看不到宣告支援的最低版本 3.11；發版前要用 3.11 跑一次 `tests/run_all.py`。

## v1.3.0 (2026-09-10)

**這一版做完的三件事**：壓縮後不再失憶（壓縮前的原文地圖會在下一回合交回模型）；夜裡的整理會順手收割新素材，只產草稿不動正式卡；規則卡可以只給某一個宿主，核心塊因此不必兩邊一模一樣。另有喚回改端卡片、開場注入歸零、行為層退出產品、核心塊改由規則卡生成等 09-07～09-09 的累積。
- compact 鏈（U-R1）：壓縮後把「壓縮前原文地圖」的路徑交回模型。目的地算法搬進 `epitype/compact_map.py`（`map_destination(vault, session_id, transcript_path)` 純函式，`session_component` 與 `_hook_common` 同規則、由 `tests/compact_map_destination_regression.py` 釘住不許漂），PreCompact 與 SessionStart 共用同一份。SessionStart 只在 `source=compact` 且事件帶 `transcript_path`、且算出來的地圖檔**存在**時，加一行 `壓縮前原文地圖：<絕對路徑>；需要原文時讀它按行號回撈。`——不猜、不用 mtime 找最新的一份、不列目錄；整行超過 240 B（UTF-8）就整行不注，路徑一個字元都不截。其他 source 不加此行，既有的 compact 抑制（不起夢、不印夢通知、不派翻譯）與非 compact 場的既有通知（card_lint FAIL、翻譯任務、夢通知）全部照舊。PreCompact 不再回傳「地圖已落於…」那句 context——它在兩邊宿主都到不了模型（Claude Code 的 PreCompact 不能注入，2026-08-19 實證；Codex 0.153 的 `PreCompactOutcome` 只有 Continue／Stopped），地圖照寫、清 recall 標記與 `_sweep_maps` 照做。selftest：compact_map 9→12、precompact 8 案改釘「寫檔但不出聲」、sessionstart 25→29（兩種宿主形狀＋`sessionId` 拼法各一行且只有一行、地圖不存在→無、startup／resume→無、超長路徑→無），新增 tests 10 案，run_all 49→50。**UNVERIFIED**：三條實機路徑（Claude Code `/compact`、Codex 互動 `/compact`、`codex exec` 自動壓縮）壓縮後第一個送模型的輸入是否真的含這一行，合併後才驗。
- dream／harvest（U-R2）：夢在第 4 節盤點草稿**之前**順手跑一趟 harvest，只產草稿。`harvest()` 新增 `drafts_only`（CLI `--drafts-only`）：transcript 的每一筆捕捉一律落 `_drafts/captured_pending/`，不寫 `grants`／`corrections`／`rulings`、不重建也不標舊索引（`capture.Replay(force_pending=...)` 把入庫閘關掉，判定本身不變）；文件句子照舊只進 `_drafts/decisions`。新增 `deadline`（`time.monotonic()` 截止點）：到點停在檔與檔之間，已整檔處理完的才記進 manifest，未處理的留給下一趟——游標＝harvest 既有的逐檔指紋，不是日期（日期會被「harvest 失敗而夢完成」「舊場追加」漏掉）。夢端給 `memspec.DREAM_HARVEST_BUDGET_SECONDS`（30 秒）與整體時限取小者，`--dry-run` 透傳，例外只在第 4 節多一行 errors（fail-open），counts 加 `harvest_new_drafts`／`harvest_files`，報告印「本次 harvest 新增 N 張草稿」。harvest 自己的 stdout 在夢裡被吞掉，報告流不混進 WOULD PROPOSE 日誌。
  - 設計依據：Claude↔Codex 兩輪收斂（`DISCUSS_COMPACT_CHAIN_20260910`，收斂共識 3）；Codex 指出現行 harvest 會直接寫正式庫並重建索引，`--apply` 只管 `--reevaluate`，所以必須先有明確的只產草稿模式。
  - 測試：harvest selftest 23 → **26**（drafts-only 只出現 pending＋正式目錄與索引都沒生出來、同一組句子不帶旗標本來就會入庫、時限停在檔間且 manifest 只含已處理檔並由下一趟接手）；dream selftest 65 → **68**（第 4 節真的有跑 harvest、`--dry-run` 一個檔都不寫、harvest 例外只多一行 error 其他節照跑）。
  - 假家目錄實跑（`HOME`／`USERPROFILE`／`EPITYPE_CONFIG` 全指暫存）：`--dry-run` 印「本次 harvest 新增 1 張草稿（--dry-run：只算不寫，掃過 1 個 transcript）」且假庫零新檔；不帶 `--dry-run` 再跑一次，新卡只有 `_drafts/captured_pending/20260909/correction-*.md` 一張，`grants`／`corrections`／`rulings` 與 `.epitype/memory_fts.sqlite3` 都不存在。閘門：run_all 49/49、privacy PASS、corpus 330/330、seeds 15/15 與 5/5。
- core-gen 宿主區（U-R3）：規則卡新增選填序列欄 `hosts`（值域 `claude`／`codex`，缺＝共用）；帶 `hosts` 的 `resident` 卡不進共用區，改排進 `## C. host zones`，一個宿主一個 `<!-- HOST x BEGIN/END -->` 註解區（標記字串在 memspec），一張卡列兩個宿主就兩區各出現一次。`card_lint`：值域外的宿主名 FAIL（`hosts`）、`floor` 層帶 `hosts` FAIL（`hosts-layer`）——底線兩邊都要。
- `core_gen.host_view(text, host)` 是給下游同步腳本的純函式：回「共用區＋該宿主區、其他宿主區與標記全部拿掉」的文字；宿主值域外、標記不成對、區外有內容都丟例外而不猜。`--cap-bytes` 改量**任一宿主實際載入的最大值**（共用區＋自己那一區），不是整份檔案——兩個宿主區的位元組沒有任何一場會同時付；摘要行多 `loaded=claude:n,codex:n` 與 `host-only=n`，核准包多每張卡的 `hosts` 與 `host_bytes`／`host_loads`。序列欄位的讀法收斂到 `memspec.sequence_items()`（`card_lint._forbidden_items` 改為呼叫它，行為不變）。
- 回歸：**一張卡都沒帶 `hosts` 的庫，輸出與加這個功能之前逐位元組相同**（說明行不接 `host-only`、沒有 C 區、`host_view` 原樣回傳）；對通用庫真卡實測與現行 `AGENT_CONTRACT_CORE_GENERATED.md` 逐位元組相同。selftest：core_gen 18 → **24**、card_lint 45 → **47**、memspec 10（併進既有的型別表案例）。
- exam（U-P3）：`_emit_results()` 把 U-P2 標的純機制題（`cards: []` ＋ `mechanism` 標籤，如 encoding／budget／fail-open／advisory）從 UNMAPPED 分出來，總結行改 `MAPPED a/total | MECHANISM b/total | UNMAPPED c/total`，`exam_results_latest.json` 每題多 `mechanism` 欄（無則空字串），不改判分；selftest 11→12 案；真庫實測 corpus_300 MAPPED 140／MECHANISM 190／UNMAPPED 0（330 題）、seeds 6／9／0（15 題）、seeds_cr 5／0／0（5 題）；閘門 run_all 49/49、privacy PASS、corpus 330/330、seeds 15/15 與 5/5。
- 整潔（U-J2）：閘門紀錄與 `forbidden` 正則驗證器搬進 `adapters/claude/_hook_common.py` 並改公開名（`compile_bounded_regex`、`append_gate_log`、`with_session`、`GATE_LOG_MAX_BYTES`），Stop 閘不再跨 adapter 借 `pretooluse_gate` 的私有名。純搬家：函式本體逐位元組不變，紀錄格式與輪替門檻（2 MiB → `.1`）照舊。
- gitignore: ignore the transient .hook-trust-*/ directory that hook_trust selftest creates in the repo root (it made the worktree-clean gate flap while another selftest was running).

### U-K3：夢第 10 節認人審過的標記（FAILURE_MODES §42）

- 病灶：拆卡候選只看三個機械訊號（正文 ≥2 個 `## `、正文 > `CARD_BODY_MIXED_BYTES`、description 又長又用「＋」「；」串），不認人的判斷。owner 2026-09-09 在 titan 庫逐張審完 89 張並標 `mixed_reviewed: 2026-09-09-keep`／`-index`、拆掉的原卡標 `status: superseded` 之後，下一次仍列 **122** 張（原 89 全數回榜，加上剛拆出來、繼承了母卡 description 的新卡）。清單清不掉就只剩兩條路：做一份永遠不會變短的工，或整節不看。
- **判定**（`dream._mixed_skip_reason`）：三種情形略過並計數而不列出——frontmatter 有 `memspec.MIXED_REVIEWED_FIELD`（`mixed_reviewed`，**值一律不讀**，只看鍵在不在，所以任何語言的標記都算數，也沒有任何日期或詞彙被寫死）、卡是 `status: superseded`、或卡有 `memspec.SPLIT_FROM_FIELD`（`split_from`）且現在只有不到 `CARD_MIXED_HEADING_MIN` 個小標又不超上限。最後一條只赦免 description 那一條：**剛拆出來的新卡若自己長了兩個小標或超上限，照樣列**，那是新卡自己的問題。
- 略過**只在「這張本來會被列」時計數**，報告多一行「已審過略過 N 張」，counts 另有 `reviewed_skipped` 與 `skipped_by_reason` 明細。把沒上榜的卡也算進去，N 就對不上清單的前後差，那一行會變成不能查的數字。
- **真庫實測**（唯讀 `--dry-run --json`，`HOME`／`USERPROFILE`／`EPITYPE_CONFIG` 全指暫存，2026-09-10）：titan 庫 **122 → 22**，略過 100（reviewed 89、fresh_split 11）；治理庫 **75 → 11**，略過 64（全部 reviewed）。兩邊都恰好對得起來（122−100＝22、75−64＝11）。治理庫改前是 75 而不是派工單寫的 71——那是 U-K 當天的數字，這幾天新卡讓它長了 4 張。
- **沒動的**：三個訊號的門檻與判定、五十列上限、夢不自動拆卡、任何寫檔行為。標記住在卡片裡，是人放的；把 `mixed_reviewed` 拿掉，那張卡下一次就回到清單上。
- 測試：dream selftest 60 → **65**（已審過略過、superseded 略過、剛拆的新卡不回榜但同樣 description 的無標記雙胞胎照樣列、長出兩個小標的拆出卡再次被列、報告的數字＝從清單上被拿掉的張數）。閘門：run_all 49/49、privacy PASS、corpus 330/330、seeds 15/15 與 5/5。

### U-H：喚回只端卡片，原話留在最底層（owner 2026-09-09；FAILURE_MODES §41）

- owner 原話：「其他全部按需讀：索引指到卡片，卡片只在喚回時出現；卡片下面才是解說，再下面才是原話和對話紀錄，要用到才翻」。UserPromptSubmit 不再注入 `rulings/`／`corrections/`／`grants/` 的原話事件檔——那是第四層（最底層）的原始紀錄，只在 AI 主動 `memsearch` 時才出現。根因是層級錯位不是標籤錯：原話被當卡片層逐句注入，還佔了預算裁不掉的置頂席，所以沒人核過的一句話每次詞面命中就先於卡片抵達現場，「歷史捕捉」四個字改不了它讀起來像現行裁定。
- **判定**（`adapters/claude/recall_hook._event_card`）：用庫相對路徑的第一段比對 `memspec.EVENT_CARD_DIRECTORIES`，命中就在任何上限／前綴／席位計算之前跳過。**唯一例外**：該檔本身是現行決策卡（`decision_key` ＋ `status: active`，由 `_active_decision` 從卡片重讀，不信索引）時，它是「剛好住在捕捉目錄的卡片層」，照舊佔決策席、帶 owner 原話。
- 隨之退役：`_captured_context`（整份原話讀進注入行）、`_CAPTURE_HISTORY` 前綴、corrections／rulings 的置頂席與那條「裁定只命中正文不給席」的補丁、`memspec.CORRECTION_PREFIX`／`RULING_PREFIX`／`CAPTURE_LABEL_REGEX`。置頂席現在只有決策卡一種來源，`decisions`／`pinned` 兩份清單併回一份。
- **完全沒動**：捕捉寫入（U-B 白名單、U-P 的 `event_id`、提案區）、`memsearch` 的索引與 CLI（原話照樣搜得到，這正是它的到達路徑）、Stop 決策閘、寫檔閘。夢第 12 節照舊列出沒有卡片承接的原話——移除注入之後那一節是唯一會指出缺口的地方。
- **考題改判 1 題、刪 0 題**：`corpus_300.json` 的 `recall-p1-004` 原本期待「⚖ owner 裁決：」出現在置頂視窗，改判為決策卡照舊置頂、`rulings/004-ruling.md` 不得進視窗。為此 `exam/exam_runner.py` 的 recall 判分新增 `pinned_excludes`（`top_k_excludes` 表達不了「搜得到但不得注入」這半件事），隨箱 `exam/sample_corpus.json` 的 `recall-zh-pinned` 同時補上一張原話卡當回歸。`seeds_*` 兩份的事件卡題全部是 `top_k_contains`（量索引，不量注入），一題都沒改。
- **實測**（兩真庫唯讀複本＝治理庫 435 張、titan 390 張，5 個 prompt，走 `~/.epitype/hooks/recall.py` shim，HOME／EPITYPE_CONFIG 全指暫存）：每句注入位元組 4598／3245／5349／2683／3012 → 2537／2399／1394／2297／2742，合計 **18887 → 11369 位元組（−39.8%）**。行數多半不變（10 行）——空出來的席位由普通卡補滿；只有「小單期」那句由 9 行降到 6 行（原話佔掉 5 席），位元組降 74%。
- 回歸：`recall_hook --selftest` 46 → **49**（原話不注入、memsearch 照樣搜得到、住在 `rulings/` 的決策卡照舊置頂）、`exam_runner --selftest` 11/11、`tests/capture_recall_regression.py` 8 → **7 案**（整支由「原話怎麼端出來」改寫成「原話不端、但搜得到」）、`tests/capture_admission_regression.py` 9 案、`tests/recall_selection_regression.py` 13 → **14 案**。`tests/recall_regression.py` 量的是索引排序不是注入，數字不變（RECALL@8 45/51＝88.2%），只補上事件檔那幾題現在量的是「搜不搜得到」。
- 閘門：run_all **49/49**、privacy PASS、corpus 330/330、seeds 15/15 與 5/5。

### U-I-b：開場不再回音短入口索引（owner 2026-09-09；FAILURE_MODES §40）

- owner 原話：「原生功能就會你就會去讀claude.md;CODEX就會去讀agents.md」「不應該塞，這是多餘設計」「沒必要就拿掉阿，反正浪費token的行為都不應該」。前置條件先接通再拆：索引分區已由同步工具寫進 `~/.claude/CLAUDE.md`、`~/.codex/AGENTS.md` 與 titan 專案的 `AGENTS.md`（本單開工前逐檔確認），宿主每一場自己載入，hook 再回音一份就是同一段文字付兩次錢。
- 移除整條索引回音路徑：`sessionstart_hook._index_echo`／`_claude_native_index_vaults`（Claude／Codex 宿主判斷）／`_joined`／`_fits`／`_SUFFIX_RESERVE`，以及 `payload_fits` 與 `epitype.capture_route` 兩條 import；memspec 退役 `slim_index` 與 `SESSIONSTART_INDEX_TRUNCATED_LINE`。祖先庫索引回音一併消失（它走同一個迴圈）。
- SessionStart 現在只剩三種行：卡片型別 FAIL、順手補中文別名、夢通知。**沒有一件事要做的那一場整段不注入**（`additionalContext` 可為空，空的就整個不輸出）；stdout 的 JSON 形狀不變（`hookSpecificOutput` → `hookEventName`＋`additionalContext`）。庫選取（cwd 庫＋治理庫）保留——它現在界定卡片檢查掃哪幾個庫，而不是回音哪幾本索引。
- 真庫副本實量（兩庫、四種事件形狀，依序 Claude×`C:\`、Codex×`C:\`、Claude×titan、Codex×titan）：改前（master `013ec77`）**0／996／996／2624** bytes，改後**全部 0**。真設定＋真家目錄的實機唯讀跑同樣四形狀皆 0 bytes、`rc=0`、stderr 空，兩庫的 `dream_state.json` 與 `no_chinese_cursor.json` sha256 跑前跑後相同（零寫入）。零是當天的實情而不是地板：當天兩庫都沒有卡片型別 FAIL、沒有缺中文別名的卡，夢也在間隔內；有 FAIL 的庫照樣出它那一行。
- 測試：sessionstart selftest 30/30 → **25/25**（`slim_index` 兩案、超預算取樣兩案、宿主形狀回音兩案退役；新增「索引一個字都不回音」與「Claude 與 Codex 形狀拿到逐位元組相同的注入」——後者差一個位元組就表示還有宿主專屬分支活著）。另有六案原本拿「索引那一行還在」當存活錨，改錨到一張過不了型別合約的卡上：context 空掉之後，「某某不在裡面」在空字串上恆真，等於什麼都沒驗到；`tests/governance_regression.py` 的降級場同理改錨。
- **考題退役：0 題**——`exam_runner` 只驅動 UserPromptSubmit／PreToolUse／Stop，不經 SessionStart；三份共享題庫掃過無一題依賴索引回音，分母不變。
- 閘門：run_all **49/49**、privacy PASS 122 檔、corpus 330/330、seeds 15/15 與 5/5。

### U-M-a：規則卡型別與核心生成器（owner 2026-09-09 最終方案；FAILURE_MODES §39）

- owner 原話：「契約應該是簡單扼要規則」「契約等應該跟整個epitype做整合」。契約長成 18 KB 的散文，一條規則與它的解說、事故、例子混在同一段，所以數不出來、退不掉、也算不出每場的固定成本；而它又活在治理其他所有durable 陳述的那套機制之外（卡片有必填欄位、lint、取代鏈、目錄與上限檢查，契約只有一個檔和一個編輯習慣）。本單只做**產品端**：卡片型別＋生成器＋檢查。**不建真卡、不寫任何契約檔或宿主檔**——哪些句子成為卡、生成器指向哪個檔，是本機作業（U-M-b）。
- **`rule` 卡型別**（`metadata.type: rule`）：必填 `layer`（`floor`｜`resident`｜`situational`｜`recall`）、`section`、`order`（整數）、`text`（核准原句、單行、任何語言）、`decided_by`（值域與 owner-explicit 要 `owner_quote` 都與決策卡同一份實作 `card_lint._decider_findings`）、`approved_by`、`approved_at`（ISO 日期）、`aliases`（≥1）；選填 `source_anchor`、`incidents`，`status`／`superseded_by` 沿用決策卡的取代鏈語意。card_lint 新增四項 FAIL：`layer` 值域外、`order` 非整數、`text` 超過 `memspec.RULE_TEXT_MAX_BYTES`（400）、`text` 不是單行。`approved_by`／`approved_at` 直接列為必填（涵蓋「floor／resident 缺核准＝FAIL」那條要求），生成器另有一道同樣的拒絕。
- **`epitype core-gen <vaults...> --out FILE [--cap-bytes N] [--dry-run] [--check] [--json]`**（`epitype/core_gen.py`）：只讀 `type: rule` 且非 `superseded` 的卡；`floor` 依 `order` 排進「## A.」編號段，`resident` 依 `section` 分「## B.」小節（節序由節內最小 `order` 決定），`situational`／`recall` 不進輸出。**只組裝不改字**：每張卡的 `text` 逐位元組照抄，除標題、說明、小標與清單符號外，輸出裡沒有任何一個字是產品寫的（模板全在 memspec，語言中立、不含任何規則）。說明行刻意不帶時間戳——帶了的話 `--check` 每次都會不同。
- 兩種拒絕都是**一個位元組都不寫**：生成層的卡缺 `approved_by`／`approved_at` → 列出那幾張；組出來超過上限（`--cap-bytes` 優先，其次設定的 `core_cap_bytes`，沒設就不擋）→ 列最長的 10 張卡。拒絕回 exit 1，工具自己壞掉回 2。
- 成功生成同時寫核准包 `<vault>/.epitype/core_gen_latest.json`：每張卡的庫／路徑／層／節／序／核准者／`text` 的 sha256，加輸出檔的 sha256、位元組、當時的上限與時間。輸出檔與核准包都走 tmp＋`os.replace`。
- **`--check`** 走同一條組裝路徑與現有檔逐位元組比對，不寫任何檔案；不同回非零。夢第 11 節對每個 `core_files` 順帶跑同一個判準（`core_gen.drifted`），把漂移列成 report-only 候選並多一條下一步。刻意的靜默：**庫裡一張規則卡都沒有就不比對**——空的組裝對上有內容的檔會是每晚一則固定的假候選。
- 視圖：規則卡列進 `_views/current.md`，並在型別段內按 `layer` 分小節（`memspec.VIEWS_RULE_LAYER_HEADING`）、節內按 `order`；值域外或沒寫 `layer` 的卡歸到 `-` 小節而不是消失（lint 已經判它 FAIL，目錄再藏起來就找不到要修的卡）。
- 同源整理：設定檔路徑與整份讀取移到 `memspec.config_path()`／`memspec.config_options()`，`dream` 的兩支改為委派——夢的上限檢查與核心生成器讀的必須是同一個檔，否則 `core_cap_bytes` 會在一支工具眼裡有設、另一支眼裡沒設。`card_lint` 的 `decided_by` 判定抽成 `_decider_findings`，決策卡與規則卡共用。
- 審核抓到一件，同批修掉：`core_gen._selftest` 沒有隔離環境，走 `main()` 的 CLI 案例會讀這台機器的真設定檔——今天綠只是因為真設定還沒有 `core_cap_bytes` 鍵，owner 依 §11 設下去的那天自測就會被真上限擋成 over-cap。改成與 `dream._selftest` 同一套：`HOME`／`USERPROFILE`／`EPITYPE_CONFIG` 指進自己的暫存目錄並在 `finally` 還原，另加一案釘住這件事（18/18）。
- 閘門：run_all 48/48 → **49/49**（新增 `epitype/core_gen.py`）、privacy PASS 122 檔、corpus 330/330、seeds 15/15 與 5/5（同一組閘門在 `EPITYPE_CONFIG` 指向一個帶 `core_cap_bytes: 5` 的設定時同樣全綠）。selftest 分母：core_gen **18/18**（新）、memspec 9 → **10**（型別表兩張都要有這個型別）、card_lint 43 → **45**、views 12 → **13**、dream 58 → **60**（本單 rebase 到 U-P 之後的分母）。

### U-P：回饋檢討機制的前四行（owner 2026-09-09；FAILURE_MODES §38）

- owner 原話：「應該有回饋檢討機制，你跟CODEX設計一下」。設計由 Claude↔Codex 收斂（`_materials/DISCUSS_FEEDBACK_REVIEW_LOOP_20260909.md`）：**回饋保留出處、夢整理候選、候選滿額才集中檢討、owner 一包核決；每場不加任何必做動作。** 本單做落地十行的第 1–4 行，全部純程式、report-only、不改卡、不注入對話、不新增每場成本。
- **事件識別與去重**（`epitype/capture.py`）：捕捉卡新增 `event_id`（宿主＋對話＋訊息位置＋文句的穩定雜湊）與 `origin`（同一份身分的可讀式 `host/session/position`），檔名由 `{kind}-{日期}-{digest}.md` 改為 `{kind}-{日期}-{digest}-{event_id}.md`。去重規則由「同文句不重寫」改為「同 `event_id` 不重寫，同一場對話裡的同文句仍只留一張，跨場各留一張」；判不出來源（session 為空）時退回舊的整庫比對。既有事件檔不動，`existing_capture` 用 `-{digest}*` 比對所以舊卡照樣找得到。宿主由轉錄檔落點判（`.codex/…/rollout-*` vs `.claude/projects/…`），Codex 形狀缺 `session_id`／`sessionId` 時退回 rollout 檔名——**不依賴 SessionEnd**。回放（`harvest`）把轉錄檔路徑與行號一起餵進同一支識別函式，所以線上與回放算出同一個 `event_id`。
- 連帶修掉一個真缺陷：`harvest._move_card` 用「猜得出的檔名」判「已經在對的地方」與「目的地被佔了」，檔名帶了事件識別之後兩件事會一起錯（庫裡的卡被重新命名、「一句兩卡」被當成沒撞名）。改用 kind＋digest 判（新增 `_is_card_for`）。
- **考題對卡映射**（`exam/exam_runner.py`）：每題結果帶出題目自己宣告的 `cards`／`card`／`decision_key`，總結多一行 `UNMAPPED n/total`（本機現況 **350/350 未映射**，三份共享題庫一題都沒改）。`setup.vault_cards` 刻意不當映射——那是這一題的合成庫，拿它充數會讓未映射數永遠是 0。`run_corpus` 的每筆結果由 2-tuple 改為 `(id, failure, cards)`。新增 `--dry-run`；非 dry-run 且 `EPITYPE_CONFIG` 有指路時，另寫 `<治理庫>/.epitype/exam_results_latest.json`（題號、通過／失敗、cards、`rules_version`＝題庫檔 sha256 前 12 碼）。沒有指路就印 `RESULTS SKIPPED`，不猜真庫。
- **夢第 15 節「檢討包 / review pack」**（`epitype/dream.py`）：四種來源對齊到卡上——事件卡（`matched_card`／`event_id`／`verified`）、`_GATE_LOG.jsonl` 的 `stop_block`／`write_block`（沿用 `gates_report.load_rows`，數字與擋下報告同源）、`exam_results_latest.json` 的失敗題、第 8–12 節候選數（只當背景）。每張被指到的卡一列：事件數（去重後）｜未核事件數｜擋下數｜考題失敗數｜最近日期。「待判問題」＝列數；達 `memspec.REVIEW_PACK_TRIGGER`（5）時 `_next_steps` 加一行「檢討包達門檻」，未達則寫「未達門檻（n/5）」。**不判型別、不改卡、不動層、不進 SessionStart**（開場只讀 state 的 headline 欄，那三欄沒動）。第 8–12 節候選與「對不到卡的事件」只顯示不計入門檻：兩者都會把同一件事算兩次，而且在真庫規模下會讓門檻永遠成立。
- 編號：第 13（整形）與 14（下一步）不動，檢討包接在第 14 節之後印，號碼與閱讀順序一致。`build_report` 的節次改由一個 `run()` 包裝跑，第 15 節最後跑（它要讀前面算完的候選數）；`_report_errors` 改走 `_SECTION_IDS`，時限耗盡時 13 節都留缺口紀錄。
- 真庫實測（`--dry-run`，唯讀）：治理庫 3 列／3 件（91 則事件、29 則 `verified: false`、**91 則無 `matched_card`**、15 次擋下），專案庫 1 列／1 件（53 則事件、12 未核、53 無映射、1 次擋下），兩庫都未達門檻並註明「沒有 exam_results_latest.json」。`matched_card` 欄目前沒有任何路徑會寫——那是收斂 #2 的選擇（糾正明確指到規則時才寫），缺口寫在節的備註裡而不是折算成 0。
- 回歸：`dream.py --selftest` 48 → **53**（五種來源各一、event_id 去重、門檻兩側、渲染順序與「一個檔都沒動」的位元組比對）、`exam_runner --selftest` 7 → **11**、`recall_hook --selftest` 46/46（四處檔名 glob 改 `-{digest}*`，兩處「跑兩次」的固定 session 由隨機改為同一個——換場本來就該各留一張）、`harvest --selftest` 23/23、`tests/capture_integration_regression.py` 6 → **8 案**、`tests/capture_admission_regression.py` 8 → **9 案**。run_all **48/48**、privacy PASS 121 檔、corpus 330/330、seeds 15/15 與 5/5。

### U-J：拆掉傷疤卡 trigger 的機械攔截（owner 2026-09-09；FAILURE_MODES §34）

- owner 裁定原話：「我認為沒有所謂攔截層，應該都是變成類似規則或記憶卡，沒必要多設計攔截層出來」「機械阻斷<-這個就是多餘設計，我認為這種就是核心記憶」。PreToolUse 不再讀卡片的 `trigger:`、不再比對工具名與指令字串、不再因此擋下任何一次呼叫；留下的只有寫檔內容閘（規則 A 現行裁定 `forbidden`、規則 B 卡片型別合約）與它的 `_GATE_LOG.jsonl` 稽核。**不可逆動作交給宿主原生規則**（Claude `permissions.deny`、Codex `execpolicy` `~/.codex/rules/epitype_guard.rules`），在呼叫發生前就拒絕。
- 根因：那是第三層。字串樣式當不了 shell 解析器（§24 就是收據：擋 `reset --hard` 的樣式同時擋掉 clone 的 `--no-checkout` 與一段只在做診斷的正則），成本卻攤在每一次工具呼叫上，而且一個打錯的 trigger 就能把守衛靜靜關掉。
- 移除：`_trigger_card_paths`／`_parse_trigger_card`／`_declares_trigger`／`_write_trigger_cache`／`_inline_mapping`／`_bounded_deny`／`_append_audit`／`_append_parse_defect` 與整組命令解析（`_command_candidates`、`_shell_segments`、heredoc 剝除、`python -c` 內嵌命令、命令位置判定、`SHELL_TOOL_NAMES`）。常數移除 `memspec.TRIGGER_TOOL_FIELD`／`TRIGGER_INPUT_FIELD`／`TRIGGER_MATCH_FIELD`／`TRIGGER_COMMAND_MATCH`／`TRIGGER_FULLTEXT_MATCH`／`TRIGGER_TOOL_PATH`／`TRIGGER_INPUT_PATH`／`GATE_DEFECT_NOTICE`；`TRIGGER_REGEX_MAX_CHARS` 更名 `FORBIDDEN_REGEX_MAX_CHARS`（它現在只服務 `forbidden`）。`<vault>/.epitype/gate_triggers.json` 與 `gate_parse_defect_seen.json` 不再讀寫，既有檔留在磁碟不刪。
- 保留但改名／搬家：防災難性回溯的正則驗證器 → `pretooluse_gate._compile_bounded_regex`（兩道閘讀 `forbidden` 的同一份）；YAML flow 逗號切分器 → `memspec.split_flow_items`（Stop 閘與 `dream.py` 本來就跟 trigger 解析器借這一支）。
- 卡片契約：`scar` 型別的必填欄位由 `trigger.tool`／`trigger.input`／`advice`／`incident` 縮為 `advice`／`incident`；`trigger` 不再是型別的結構訊號，卡片還留著它只換來一則 `card_lint` WARN（`deprecated-field`，語言中立模板 `memspec.DEPRECATED_FIELD_REASON`），不是 FAIL。
- 退役：`tests/git_gate_regression.py`（整支只驗命令比對，31 個 git 指令形狀）；`tests/capture_admission_regression.py` 的第 3 條消費端查證（其餘四條保留）。範本四張示例卡保留 incident 與 advice、去掉 trigger，`templates/power/examples/scar-destructive-git.md` 改記「為什麼字串樣式是錯的形狀」。
- 考題：**89 題改判、0 題刪除**。graded 題庫裡每一題 `gate` 都是「裝一張 trigger 卡→期待 deny」，現在全部反過來成為本單的回歸——`corpus_300.json` 80 題中 59 題由 `deny` 改 `allow`（另 21 題本來就是 `allow`），`seeds_20260902_morning_review.json` 9 題中 5 題。分母不變。隨箱 `exam/sample_corpus.json` 的三題 gate 就地改建在寫檔閘上（中英各一題命中現行裁定 `forbidden` 而被擋、一題照裁定寫而放行），總題數仍 16。
- 順手修掉的可重跑性缺陷：寫檔閘對同一組（session、規則、檔案、內容）只擋一次，同一份題庫連跑兩次時第二次會變成放行。`exam_runner._run_gate` 改為每題自己一個 session id、跑完清掉標記（與 `_run_stop` 同一套；新增 `_hook_common.clear_notice_markers`）。
- 閘門：run_all 由 49/49 降為 **48/48**（退役一支回歸）、privacy PASS、corpus 330/330、seeds 15/15 與 5/5。selftest 分母：pretooluse 73 → **30**、card_lint 41 → **42**、exam runner 7/7、stop 22/22 不變。

### U-I-a：開場只留要動作的東西

- SessionStart 不再注入工作帳本、現行裁定塊、殭屍待辦行、「🔁 喚回的卡若與現況不符…」提醒行，以及任何固定說明文字（owner 2026-09-09：「不應該塞，這是多餘設計」「原生功能就會你就會去讀claude.md;CODEX就會去讀agents.md」「現行裁定清單（12 行）我認為應該是回歸進入正確地方」「沒必要就拿掉阿，反正浪費token的行為都不應該」；FAILURE_MODES §35）。帳本檔本身不動——它仍是治理庫的辨識標記；裁定改由喚回在命中時帶回（帶 owner 原話），待辦由 `epitype pending` 與夢點名。
- 卡片型別檢查改成只有 FAIL 才出一行（WARN 的數字照舊附在同一行）；只有 WARN 的庫在開場不出聲。
- 夢的一行只在要有人動手時出現：夢報錯、夢列了待審候選、夢到期沒跑（判準與起夢同一份 `interval_hours`，lock 未逾時＝正在跑就不算沒跑）。乾淨跑完那一場不再出聲，`DREAM_NOTICE_CLEAN_LINE` 由新的 `DREAM_NOTICE_OVERDUE_LINE` 取代。
- 真庫副本實量（兩庫、四種事件形狀，Claude／Codex × cwd `C:\` 與 titan）：改前 8015／8060／8153／7986 bytes，四種形狀全部撞到 8 KB 上限並以「…（超出預算，餘 N 段未注入）」結尾，其中兩種形狀連短入口回音都被擠掉；改後 0／2033／1821／3510 bytes，短入口回音不再被擠。索引回音、壓縮後復原、夢 spawn、fail-open、預算與 shim 契約都沒動。
- 退役：`pending_lint.summary_line`（2 項 selftest 隨之退役，7/7 → 5/5）、`sessionstart_hook` 的 `_active_decisions`／`_decision_block`／`_vault_labels`／`_frontmatter_fields`，以及 memspec 的 `SESSIONSTART_DECISIONS_HEADER`／`SESSIONSTART_DECISIONS_MAX_LINES`／`SESSIONSTART_DECISION_RECENT_DAYS`／`SESSIONSTART_DECISION_REST_LINE`／`CARD_SELF_CORRECT_NOTICE`／`PENDING_LINT_HOOK_BUDGET_SECONDS`／`DREAM_NOTICE_CLEAN_LINE`。`epitype/ledger_gate.py` **不退役**：它是 `epitype ledger append` 的證據閘，與開場注入無關，仍有 CLI 與 package_smoke 兩個消費者。
- 測試：sessionstart 31/31 → **30/30**（四案由「有這一行」改寫成「永遠沒有這一行」，被移除行為的專屬案例一併退役）、card_lint 42/42 → **43/43**（新增「只有 WARN 不出聲」）、pending_lint 7/7 → **5/5**；`tests/governance_regression.py` 的降級場改以短入口回音驗注入仍送得出、`tests/dream_status_regression.py` 改驗乾淨的夢不出聲、`tests/capture_admission_regression.py` 的第 4 項（開場裁定清單）隨功能退役（同批的第 3 項已由 U-J 退役，該題現在只剩三條消費端）。run_all **48/48**、privacy PASS 121 檔。
- **考題退役：0 題**——三份題庫都不經 SessionStart（exam_runner 只驅動 UserPromptSubmit／PreToolUse／Stop），corpus 330/330、seeds 15/15 與 5/5 分母不變。
### U-K：夢多五節「只列候選」的盤點（owner 2026-09-09；FAILURE_MODES §36）

- 裁定原話（owner 2026-09-09）：夢負責「讓沒經過確認的東西回到該在的層或卡」、「一張卡就是一個記憶或規則，不要混雜」、「我不知道會有多少地方在產生非整理的記憶」；另裁上限＝檢討值＋兩成，超過即由夢整理（此句為派工單轉述，非逐字原話）。五節**全部 report-only**：夢不搬、不改、不刪、不拆、不升卡，處置一律是人的動作。
- **第 8 節 全庫掃描與口袋庫**：除 config 登記的庫以外，掃 `<家目錄>/.claude/projects/*/memory/`，把「未登記但含 ≥1 張 `*.md` 卡」的目錄列成歸戶候選（路徑、卡數、最新 mtime）。家目錄不寫死：先從登記庫自己的路徑往上認 `.claude/projects`，認不出來才退回 `HOME`；只認這個形狀是刻意的，改用「登記庫的祖父目錄就是根」會讓一個放在 `C:\a\b` 的庫把 `C:\` 底下每個目錄都掃一遍。只數 `*.md`、不解 frontmatter（一個 ~200 個專案的家目錄不該為此付整庫解析的錢）。
- **第 9 節 草稿老化**：每個登記庫的 `_drafts/**` 遞迴——總份數、>7 天、>30 天、依第一層子夾分組（各含 total／over_7／over_30）、最舊 5 份（相對路徑＋天數）。第 4 節數的是「有幾份待審」，這一節數的是「積了多久」；本單重測治理庫 370 份（owner 派工單提的是 369，隔一天多一份）。
- **第 10 節 混雜卡**：納管卡（沿用 memsearch／views 的掃描範圍）三個形狀訊號任一成立即列拆卡候選——正文有 ≥`memspec.CARD_MIXED_HEADING_MIN`（2）個 `## ` 小標（```圍籬內的不算，卡片會貼 markdown 範例）、正文位元組 > `memspec.CARD_BODY_MIXED_BYTES`（4000）、`description` > 160 字元且用「＋」「；」串了多件事。每庫列上限 50 條，超過只報總數。
- **第 11 節 上限檢查**：config 新增三個**選填**鍵 `index_cap_bytes`、`core_files`（絕對路徑清單）、`core_cap_bytes`。缺鍵一律寫一行「未設定」跳過，**絕不套內建門檻**——一個產品猜出來的上限被寫成「超上限」，讀的人會以為那是 owner 的判斷。超過就列（檔案、現量、上限、超出量），不改檔。
- **第 12 節 原話無決策卡承接**：每個登記庫的 `rulings/`／`grants/`／`corrections/` 事件檔，frontmatter 不是 `verified: false` 的（＝現行仍會被信任的），若庫內沒有任何 `type: decision` 卡的正文或 `source`／`superseded_by`／`aliases` 提到它的檔名或 `decision_key`，就列升決策卡候選（路徑、`captured_at`、原文前 80 字），依日期新→舊，每庫上限 30 條。這是 Claude↔Codex 收斂加的那一項：喚回會把這些卡掛上「⚖ owner 裁決：」「⚠ owner 曾糾正：」端出去（`adapters/claude/recall_hook.py`），只有自報 `verified: false` 的才降級成歷史捕捉，所以一張沒人核、也沒有決策卡承接的原話讀起來仍像現行裁定。比對用完整檔名（帶 `.md`）與 `decision_key`，不用去掉副檔名的字根——`carried` 是 `uncarried` 的子字串，用字根比對會把「沒人承接」誤判成「有人承接」。
- 報告：五節各一節 markdown，`_next_steps` 納入五項候選數（每一條都寫成「人工判斷」而不是「夢會處理」）。**編號位移**：主記憶整形由 `## 8.` 改為 `## 13.`（`memspec.INDEX_SHAPING_HEADING`），夢的下一步由 `## 9.` 改為 `## 14.`。`build_report` 多一個 `config` 參數（預設 `configured_options()`，讀不到設定就是 `{}`），逐節簽名改為 `(vaults, today, since_date, config)`。
- `--dry-run` 對真庫可跑（五節全唯讀）。回歸：`epitype/dream.py --selftest` 40 → **48**（新增八案：口袋庫只列未登記且有卡的、草稿分齡分組與最舊排序、三種混雜形狀且 ```圍籬不誤判、缺鍵寫未設定且不判斷、設鍵後列出超出量且兩個檔案一位元組沒動、只列沒有決策卡承接的那一份、下一步帶齊五項、報告渲染 8–12 節與 13／14 的位移）。selftest 現在把 `HOME`／`USERPROFILE`／`EPITYPE_CONFIG` 一起指進自己的暫存目錄並在 `finally` 還原——第 8 節會從家目錄推口袋庫、第 11 節會讀設定，不隔離就會掃到 owner 的真實家目錄與真設定。

### U-K2：夢第 12 節的承接判定補上真正在用的那兩條路（owner 2026-09-09；FAILURE_MODES §38）

- 缺陷：第 12 節問「這句原話有沒有決策卡承接」，卻只認決策卡的正文與 `source`／`superseded_by`／`aliases` 提到事件檔名或 `decision_key`——而卡片實際上是用另外兩種方式承接的：決策卡把原話逐字抄進 `owner_quote`（通用庫 19 張決策卡全部有這一欄，提名只清得掉 6 張卡），事件卡把承接者寫在自己的 `carried_by`。兩種都不留檔名，所以 §36 寫成那天兩庫的事件卡是 91／53＝**全部**被列成「無人承接」。全部命中的清單等於沒有清單。
- 承接改為三條路，任一成立就不列：**提名**（原有，完整檔名或 `decision_key`）、**逐字引用**（決策卡 `owner_quote` 與事件卡正文任一方向的子字串）、**自報**（事件卡 frontmatter 的 `carried_by`，新常數 `memspec.CARRIED_BY_FIELD`；那張卡在不在是 `epitype cards` 的題目，這一節不查）。
- 逐字引用的比對規則：兩邊都先 NFKC，再只留字母／數字／結合記號（Unicode 類別 L／N／M）、casefold——去空白、去引號、統一全形半形是同一個動作，不寫死任何一種語言的標點（真庫那一對只差正文中間多一個 `<`）。`owner_quote` 先在 Unicode 引號／括號類別（Pi/Pf/Ps/Pe，另加 ASCII `"` 與 `'`）切成片段再比對，因為同一欄常串了好幾段不同出處的原話（`「B」（Q7）；「…」`），整欄不是任何一份原話的子字串。重疊長度地板 `UNCARRIED_QUOTE_MIN_CHARS`＝12 字元（取較短的一邊）：4 個字的重疊在任兩段中文裡都撞得到，收它等於把這一節關掉。
- 第 12 節每列多一欄 `noise`（疑似雜訊）：正文命中 `memspec.EVENT_NOISE_MARKERS`（`{"probe":`、`transport`、`do not use tools`、`reply only`、`return only`、`health check`、`傳輸探針`，一律 casefold 子字串）就標記——自動捕捉會把跨 CLI 傳輸探針的整段 payload 寫成 ruling。**只標記**：夢不刪、不降級，counts 多一個 `noise_candidates`，下一步那一行附帶「其中 N 份疑似傳輸探針雜訊」。
- 真庫實量（`--dry-run`、兩庫、第 12 節 counts）：**改前 56＋40＝96，改後 29＋23＝52**。改前數字低於 §36 的 91／53，是因為其間有 41 張卡被標了 `verified: false`（本來就跳過），不是本單造成的。清掉的 44 張裡 33＋18 張是靠 `carried_by`（庫裡本來就在用的欄位，多半指向 `feedback` 卡），逐字引用命中 3 張、全部已被別條路清掉，所以它今天的淨貢獻是 0——保留是因為那是決策卡真正在用的寫法（19 張全部有 `owner_quote`），下一張這樣寫的卡沒有 `carried_by` 可退。noise 今天標 0 張：真庫已知的兩張探針卡都自報 `verified: false`，第 12 節本來就不列。
- 回歸：`epitype/dream.py --selftest` 48 → **53**（新增六案：三條承接路各一案、只列沒有任何一條路清掉的、短於 12 字的重疊不算承接、只有探針樣板被標 noise）。`_decision_reference_text` 改名 `_decision_carriers` 並改回傳 `(承接文字, owner_quote 片段)`，同一次掃描出兩份證據，不為新規則多掃一輪卡。

- 夢多一個順路任務「主記憶整形」（owner 2026-09-09：「我們不是有類似夢的機制，不就是剛好處理這個?」；FAILURE_MODES §33）：`MEMORY.md` 被 §31 修短之後會自己長回來——宿主「存卡後在 MEMORY.md 加一行」的預設、別場 session 直接編輯——而事前用寫檔閘擋會連手寫短入口本來就長成那樣的 `- [name](card.md)` 一起擋掉。改由 03:30 那場夢事後整形：允許段（`memspec.INDEX_ALLOWED_SECTIONS`＝習慣與偏好／找不到就搜／索引卡／專案規則，各含英文寫法）內一律不動；允許段以外、且連到的卡 `_views/current.md` 或 `history/closed.md` 已經列出的整行，原文照搬進 `<vault>/_drafts/index_pruned/YYYYMMDD.md`（附時間、來源段、原因），不刪。
- 視圖沒列到的連結不搬，只列進報告：那可能是幾分鐘前才寫好、目錄還沒生成的新卡。整形排在順路重生 `_views/` 之後，因為「目錄已經承載這張卡」就是它唯一的判準，判準不能是舊的。
- 寫法是受控的小範圍改寫，不是重寫：讀→記 mtime＋大小→算→寫前再比 mtime＋大小→帶原內容比對的換名寫入（`card_io.replace_if_unchanged`，取鎖）→再讀核對。任一步對不上就整份放棄、報告記一行、下次夢重試。紀錄檔先寫、`MEMORY.md` 後改，所以被拒的換名不會弄丟行；下一次靠「原文行已在檔內」去重，不疊第二份。`--dry-run` 只印會搬幾行，一個位元組都不動。
- 夢報告多一節「## 8. 主記憶整形」（原本的下一步改為第 9 節），列每庫的狀態、搬出行數、留下行數與放棄原因；下一步會提示「有連結不在目錄裡 → 跑 views」與「整形放棄 N 次」。第 4 節不再對 `_drafts/index_pruned/` 提 `harvest --reevaluate`（那條路問的是捕捉規則，跟整形無關）。
- 本機 `memory_lint.py` 未改：`INDEX_WARN_KB = 3.0` 與 `index_over` 計入 issues 已經在位（`memory_lint.py:389`／`:422`），`MEMORY.md` >3 KB 本來就列 ISSUES 並在 SessionStart 浮一行，符合本單要求。
- 審核抓到三件，同批修掉：檔首 BOM 讓第一個標題認不出來（整份檔被當成「不在任何段」，連允許段的手寫行都可搬）→ 判段前跳過 BOM，寫回位元組不變；第一個 `##` 之前的前言區改為一律不搬（短入口的標題行與說明行本來就可能帶連結）；`index_pruned` 改逐位元組照搬（CRLF 行尾原樣保留）且不再去重（同一行分屬兩段是兩件事）。
- 回歸：`epitype/dream.py --selftest` 31 → **40**（新增九案：`--dry-run` 不動檔、只搬允許段外且目錄承載的行、紀錄檔原文照搬＋來源段、視圖未列的行留著並進報告、再跑一次無動作、寫入前 mtime 變了整份放棄，加上審核補的 BOM 不動檔、前言區不搬、紀錄檔 CRLF＋重複行各留一筆）。`views.py` 的連結轉義表移到 `memspec.MARKDOWN_LINK_ESCAPES` 供生成端與還原端共用，行為不變。run_all 49/49、corpus 330/330、seeds 15/15 與 5/5。

- 自動捕捉改折衷制（owner 2026-09-09 裁 Q5「C」；FAILURE_MODES §32）：只有形狀明確的三個模板自動入庫——`arrow-answer`（有 `<-`／`<=`／`《` 回覆標記，且 owner 那半以短答開頭：同意／可／不／好／甲乙丙／A–E／yes／no）、`leading-correction`（owner 那半**句首**是不是！／不對，／不要／別再／錯了／stop）、`explicit-grant`（明示第一人稱授權：我同意／同意過／我授權／准你／批准你／允許你／你可以＋動詞／I agree／you may）。判定只看 owner 自己那半（`capture.owner_side`：裁定卡正文帶著助理的提問，不能讓助理替 owner 蓋章），常數在 `memspec.CAPTURE_ADMIT_*`。
- 其餘「現行規則仍會捕捉」的句子改寫提案：`<vault>/_drafts/captured_pending/YYYYMMDD/<原本的檔名>.md`。`_` 開頭的路徑段本來就不在 `memsearch._scan_vault` 的掃描範圍，所以提案不進索引、不被喚回、也不受寫檔閘的卡片契約管——沒有第二條排除規則要同步。線上 hook 與離線回放（harvest）共用 `capture.write_capture` 這一處判定，落點一致；`--dry-run` 多印一種 `WOULD PROPOSE`。
- 轉正是人的動作，不是回放：`harvest --reevaluate --apply` 對 `verified: false` 的提案印 **HOLD** 不搬（提案本來就是「今天的規則也會捕捉」，那正是它被扣住的原因），dream 第 4 節改列待審份數與人工審閱指令，不再對捕捉提案提供 `--reevaluate` 那條命令。
- 每張自動寫的卡（入庫與提案皆同）frontmatter 帶 `provenance: auto-captured` 與 `verified: false`；轉正時改 `verified: true` 並補 `verified_by`／`verified_at`。四個欄位列入事件卡型別的選填欄，`epitype cards` 不因此 WARN。
- 消費端逐處查證後釘進回歸：Stop 決策閘（`decision_key`＋`status: active`）、寫檔閘規則 A（同一批決策卡）與規則 B（`_` 路徑段不算卡）、PreToolUse 授權判定（只讀宣告 `trigger:` 的卡；**這一條已被同版的 U-J 取代——那條路徑整條移除了**）、SessionStart 現行裁定清單（同決策卡門檻）都吃不到 `verified: false` 的卡；喚回照舊給它 pinned 席位但只掛「歷史捕捉（非完整對話／現行裁定）」前綴，不掛決策前綴。
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
