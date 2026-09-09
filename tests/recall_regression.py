import sys; sys.dont_write_bytecode = True; [getattr(stream, "reconfigure", lambda **_: None)(encoding="utf-8", errors="replace") for stream in (sys.stdout, sys.stderr)]  # cp950 主控台先轉 UTF-8，避免繁中輸出中斷。
"""喚回品質的回歸測試：「問這句話，該被喚回的卡有沒有進視窗」。

2026-09-05 實測事故：owner 用中文問虛擬單與實單的鏡射政策，答案卡只有英文
描述，排在喚回視窗外六十名以後。當時沒有任何工具能把這件事變成一個數字，
所以「向量要不要上」的裁定條件（量測不達標才升）無法判斷。這支測試就是那
把尺。

判分：一題給一句 prompt 與期望卡，期望卡出現在前 RECALL@8 名（＝
memspec.RECALL_TOTAL_MAX_LINES，喚回注入的總行上限）就算命中。題型三種：
  expect       期望卡必須進前 8（可給多張，任一張命中即算）
  forbid       這張卡不得出現在候選視窗裡（被取代卡的正常結果）
  expect_empty 整句話不該喚回任何卡（棄答題）

量測單位是「單一 vault」。真機的喚回閘會把多個 vault 的結果併成一份注入，
本測試不模擬那層併合——要量的是索引與排序找不找得到，不是注入預算怎麼分。
U-H 之後這條界線多了一項實質差別：`rulings/`／`corrections/`／`grants/` 的
原話事件檔仍在索引裡，但喚回一律不注入，只在 AI 主動 memsearch 時端出。
期望卡是事件檔的題（mix-scratch、mix-bugfix-grant，以及與規則卡並列的那幾題）
量的因此是「搜得到嗎」，不是「會不會自動送到眼前」。

用法：
  recall_regression.py --selftest              內建合成 vault 與題庫
  recall_regression.py <題庫.json> [--strict]  跑外部題庫（可指向真實 vault）

題庫 JSON：{"vault": "<路徑>", "min_recall": <0..1>, "questions": [
  {"id": "...", "prompt": "...", "expect": ["卡名或 card_path"],
   "forbid": [...], "expect_empty": false, "vault": "<這題另指 vault>"}]}
"""

import argparse
import json
from pathlib import Path
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from epitype import memsearch, memspec  # noqa: E402

# 命中的門檻＝喚回注入的總行上限：排在這之後的卡片，在真機上等於沒被喚回。
# （事件檔按上面的說明另計：它們量的是 memsearch 搜不搜得到。）
RECALL_AT = memspec.RECALL_TOTAL_MAX_LINES
# 診斷要說出期望卡「實際排第幾」，所以候選視窗開到遠大於 RECALL_AT；排序是
# 先排序後切片，取 200 再切前 8 與直接取前 8 完全同序。
DIAGNOSTIC_WINDOW = 200
# 合成題庫的門檻＝2026-09-06 實測值（45/51＝88.2%）。調低這個數字等於把回歸
# 測試關掉；要動它只能因為題庫本身改了，並且在同一次改動裡附上改前改後的數字。
SYNTHETIC_RECALL_MIN = 0.882


def _card_text(card):
    lines = ["---", f"name: {card['name']}", f"description: {card['description']}"]
    if card.get("aliases"):
        lines.append("aliases: [" + ", ".join(card["aliases"]) + "]")
    if card.get("status"):
        lines.append(f"status: {card['status']}")
    if card.get("superseded_by"):
        lines.append(f"superseded_by: {card['superseded_by']}")
    lines.extend(["---", card.get("body", "")])
    return "\n".join(lines) + "\n"


# 72 張合成卡：純中文規則卡、純英文規則卡、雙語別名卡、被取代／後繼成對、
# 事件卡（糾正／裁決／授權），外加一組鄰題干擾卡。純英文卡刻意留著沒有
# 別名，2026-09-05 事故的形狀才進得了題庫。
SYNTHETIC_CARDS = (
    # 純中文規則卡
    {"path": "rules/schedule-retry-backoff.md", "name": "排程重試退避",
     "description": "排程任務失敗後採指數退避重試，最多三次；第三次仍失敗就換路徑不再磨",
     "body": "退避倍數為二，起始間隔十秒。第三次失敗要落一行紀錄說明換了哪條路徑。"},
    {"path": "rules/backup-before-migrate.md", "name": "遷移前先備份",
     "description": "資料表遷移前必須先做完整備份並驗證還原；沒有驗證過的備份不算備份",
     "body": "驗證還原＝把備份倒進一個空的暫存資料庫，跑一次讀取查詢。"},
    {"path": "rules/timezone-store-utc.md", "name": "時區一律存協調時間",
     "description": "所有時間戳存協調世界時，顯示層才轉當地時區；混存本地時間會讓跨日統計錯位",
     "body": "跨日統計錯位的症狀是同一天出現兩次或整天消失。"},
    {"path": "rules/log-no-secret.md", "name": "日誌不得寫入憑證",
     "description": "日誌與錯誤訊息一律遮罩金鑰、密碼與權杖；遮罩在寫入前做，不靠事後清理",
     "body": "事後清理救不了已經被輪替到備份裡的那一份。"},
    {"path": "rules/cache-invalidate-on-write.md", "name": "寫入即失效快取",
     "description": "寫入路徑必須同步標記快取失效；靠過期時間收斂會讓讀者看到舊值",
     "body": "讀到舊值的回報通常長得像「我明明存了」。"},
    {"path": "rules/lock-timeout-fail-open.md", "name": "拿不到鎖就讀舊快照",
     "description": "重建索引拿不到鎖時不得阻斷查詢，改讀既有快照；閘門壞掉不能讓功能整個停擺",
     "body": "阻斷查詢的代價是喚回整個消失，比讀到略舊的一份嚴重得多。"},
    {"path": "rules/encoding-utf8-console.md", "name": "主控台一律先轉編碼",
     "description": "輸出繁體中文前先把標準輸出轉成通用編碼，否則舊主控台會在程式進入點就中斷",
     "body": "中斷的位置在第一次列印，看起來像工具根本沒跑。"},
    {"path": "rules/alert-needs-owner-action.md", "name": "告警必須帶下一步動作",
     "description": "監控告警一律寫明誰要做什麼；只報數字的告警會被當雜訊忽略",
     "body": "一行動作句勝過十個指標。"},
    {"path": "rules/permission-least-privilege.md", "name": "權限最小化",
     "description": "服務帳號只給當下任務需要的權限，批次結束就收回；長效權杖一律禁用",
     "body": "收回的動作要進批次的收尾段，不能靠人記得。"},
    {"path": "rules/audit-keep-evidence.md", "name": "稽核證據留到結案",
     "description": "診斷用的觀測資料在問題確認解決前不得刪除；臨時工具也要留下輸出",
     "body": "刪掉觀測資料等於把下一次的重現成本全部付兩遍。"},
    {"path": "rules/rate-limit-per-caller.md", "name": "限流以呼叫方為單位",
     "description": "限流計數綁呼叫方身分而非整體流量；整體限流會讓吵的鄰居餓死安靜的呼叫方",
     "body": "身分欄位缺漏時退回位址，但要落一行紀錄。"},
    {"path": "rules/dependency-pin-exact.md", "name": "相依版本必須釘死",
     "description": "相依套件一律釘到確切版本並記下雜湊；浮動版本讓昨天過的測試今天壞掉",
     "body": "浮動版本的失敗一定發生在最忙的那天。"},
    # 純英文規則卡（無別名）
    {"path": "rules/prefer-stdlib-only.md", "name": "prefer-stdlib-only",
     "description": "Ship with the standard library only; a new third-party dependency needs an explicit decision and a removal plan.",
     "body": "A dependency that cannot be removed later is a permanent liability."},
    {"path": "rules/idempotent-writers.md", "name": "idempotent-writers",
     "description": "Every writer must be safe to run twice; a retry that duplicates rows is a defect, not a race.",
     "body": "Idempotency is asserted by running the writer twice in the test, not by reasoning."},
    {"path": "rules/no-silent-skip.md", "name": "no-silent-skip",
     "description": "A skipped step is reported, never swallowed; exit code zero is not success when the payload is empty.",
     "body": "Check the artefact the step was supposed to produce, not the return value."},
    {"path": "rules/exact-pathspec-commits.md", "name": "exact-pathspec-commits",
     "description": "Stage explicit pathspecs only; an add-all sweep pulls unrelated work into the commit.",
     "body": "The reviewer pays for every file the author did not mean to send."},
    {"path": "rules/test-behaviour-not-execution.md", "name": "test-behaviour-not-execution",
     "description": "Assert intended behaviour, boundaries and failure modes; asserting that the command ran proves nothing.",
     "body": "A test that cannot fail is a comment with a longer runtime."},
    {"path": "rules/single-writer-per-index.md", "name": "single-writer-per-index",
     "description": "One writer per index file; concurrent rebuilds tear the database and leave half-written rows behind.",
     "body": "Take the lock or read the existing snapshot; never write without it."},
    {"path": "rules/bounded-output-budget.md", "name": "bounded-output-budget",
     "description": "Injected output is truncated at a fixed byte ceiling; an unbounded report is dropped by the host instead of shown.",
     "body": "Budget the important lines first, because the tail is what gets cut."},
    {"path": "rules/fail-open-on-timeout.md", "name": "fail-open-on-timeout",
     "description": "A gate that times out must let the work through and log the miss; a blocking gate takes the product down with it.",
     "body": "The log line is what makes a fail-open gate honest rather than absent."},
    {"path": "rules/no-model-in-hot-path.md", "name": "no-model-in-hot-path",
     "description": "The hot path stays deterministic: no model call, no network, no telemetry, because its latency is paid on every prompt.",
     "body": "Anything that needs a model belongs in an offline batch."},
    {"path": "rules/resolve-fixture-roots.md", "name": "resolve-fixture-roots",
     "description": "Resolve every fixture directory before comparing it with product output; a short path name never compares equal.",
     "body": "The mismatch only shows up on the runner, never on the author's machine."},
    {"path": "rules/one-fact-per-record.md", "name": "one-fact-per-record",
     "description": "One record carries one fact; a record holding three claims cannot be superseded without losing two of them.",
     "body": "Split before writing, because splitting afterwards rewrites history."},
    {"path": "rules/mask-before-persist.md", "name": "mask-before-persist",
     "description": "Mask credential material before it reaches disk; scrubbing a stored file afterwards leaves copies in the backups.",
     "body": "Masking is a property of the writer, not of a cleanup job."},
    # 雙語別名卡
    {"path": "rules/virtual-mirrors-live.md", "name": "virtual-mirrors-live",
     "description": "A dry-run book must mirror the real book: same entry, exit, sizing and risk policy; a divergence is a defect, not a research topic.",
     "aliases": ["演練帳必須鏡射正式帳", "演練帳鏡射正式帳", "兩本帳不同是缺陷",
                 "空跑帳同正式帳標準", "演練帳要跟正式帳一樣", "dry run mirrors production"],
     "body": "只允許傳輸通道與證據來源不同，政策欄位必須逐項相同。"},
    {"path": "rules/rollback-drill-monthly.md", "name": "rollback-drill-monthly",
     "description": "Restore drills run monthly against a real snapshot; an untested restore path is an untested backup.",
     "aliases": ["還原演練每月一次", "備份要練還原", "還原路徑沒驗過", "restore drill"],
     "body": "演練要留下時間與結果，否則下次沒人知道上次練到哪。"},
    {"path": "rules/gate-quotes-owner-words.md", "name": "gate-quotes-owner-words",
     "description": "A gate that blocks must quote the decision it enforces, verbatim; a bare refusal teaches nothing.",
     "aliases": ["閘門要引用原話", "攔下來要說是哪條裁定", "擋下來要講理由", "quote the ruling"],
     "body": "引用要帶卡片路徑，讓被擋的人可以自己去看全文。"},
    {"path": "rules/measure-before-optimise.md", "name": "measure-before-optimise",
     "description": "Measure the hot path before the change and after it; an optimisation with only one number is a guess.",
     "aliases": ["先量測再優化", "改前改後都要量", "沒有數字就是猜", "兩組數字才算證據"],
     "body": "退步就回退，並在回報裡寫明退步了幾個百分點。"},
    {"path": "rules/one-task-one-session.md", "name": "one-task-one-session",
     "description": "One task is owned by one session; two sessions asking the same question spend the reviewer twice.",
     "aliases": ["一件任務一個工作階段", "同一件事不要兩邊問", "開工前先查有沒有人在做"],
     "body": "開工前先列出現有的工作階段，撞名就接手不另開。"},
    {"path": "rules/plain-language-reports.md", "name": "plain-language-reports",
     "description": "Reports are written in plain language; jargon and internal codenames are expanded on first use.",
     "aliases": ["報告要白話", "不要堆專有名詞", "術語第一次要解釋", "白話繁中"],
     "body": "代號後面補一句人話，長度不超過一行。"},
    {"path": "rules/surface-cost-before-spending.md", "name": "surface-cost-before-spending",
     "description": "Anything that eats machine resources is announced before it starts, together with its expected cost.",
     "aliases": ["會吃資源的事先講", "長跑批次要先報成本", "排程前先講會吃多少"],
     "body": "講成本的那句話要帶單位，不然等於沒講。"},
    {"path": "rules/two-failures-change-approach.md", "name": "two-failures-change-approach",
     "description": "After the same approach fails twice, stop and change route; a third grind is a decision not to think.",
     "aliases": ["失敗兩次就換路徑", "不要磨第三輪", "同一招失敗兩次就停手"],
     "body": "換路徑要寫明換到哪一條，以及為什麼那條不會踩同一顆釘子。"},
    # 被取代／後繼成對
    {"path": "decisions/retry-cap-five.md", "name": "retry-cap-five",
     "description": "The retry cap used to be five attempts.",
     "status": memspec.SUPERSEDED_DECISION_STATUS, "superseded_by": "retry-cap-three.md",
     "body": "Superseded because the fifth attempt never succeeded in the recorded runs."},
    {"path": "decisions/retry-cap-three.md", "name": "retry-cap-three",
     "description": "The retry cap is three attempts; the fourth is a route change, not another retry.",
     "status": memspec.ACTIVE_DECISION_STATUS,
     "body": "Route change means a different mechanism, not the same call with a longer timeout."},
    {"path": "decisions/index-window-five.md", "name": "index-window-five",
     "description": "喚回視窗曾為五筆",
     "status": memspec.SUPERSEDED_DECISION_STATUS, "superseded_by": "index-window-eight.md",
     "body": "五筆的時代常常把答案卡擠到第六名。"},
    {"path": "decisions/index-window-eight.md", "name": "index-window-eight",
     "description": "喚回視窗現行為八筆；超出視窗的卡等於沒被喚回",
     "status": memspec.ACTIVE_DECISION_STATUS,
     "body": "八筆是注入預算算出來的上限，不是隨手挑的數字。"},
    {"path": "decisions/hook-timeout-three-seconds.md", "name": "hook-timeout-three-seconds",
     "description": "掛鉤逾時曾設三秒",
     "status": memspec.SUPERSEDED_DECISION_STATUS, "superseded_by": "hook-timeout-ten-seconds.md",
     "body": "三秒在忙碌的機器上讓喚回被整份丟掉。"},
    {"path": "decisions/hook-timeout-ten-seconds.md", "name": "hook-timeout-ten-seconds",
     "description": "掛鉤逾時現行十秒；逾時就放行並落一行紀錄",
     "status": memspec.ACTIVE_DECISION_STATUS,
     "body": "十秒是實測的九成分位再加一倍安全邊際。"},
    {"path": "decisions/nightly-batch-opt-in.md", "name": "nightly-batch-opt-in",
     "description": "The nightly batch used to need an explicit approval every night.",
     "status": memspec.SUPERSEDED_DECISION_STATUS, "superseded_by": "nightly-batch-standing-grant.md",
     "body": "Asking every night meant the batch mostly did not run."},
    {"path": "decisions/nightly-batch-standing-grant.md", "name": "nightly-batch-standing-grant",
     "description": "The nightly batch runs under a standing grant with a hard token ceiling and a written scope.",
     "status": memspec.ACTIVE_DECISION_STATUS,
     "body": "Over the ceiling the batch skips the night and logs one line."},
    # 事件卡
    {"path": "corrections/correction-20260401-aa.md", "name": "correction-20260401-aa",
     "description": "owner correction 2026-04-01: 不要再把暫存檔寫到專案第一層，放暫存資料夾",
     "body": "第一層只放交付檔與說明。"},
    {"path": "corrections/correction-20260402-bb.md", "name": "correction-20260402-bb",
     "description": "owner correction 2026-04-02: 我說的是釘死版本，不是升到最新",
     "body": "升版是另一件事，要另外提。"},
    {"path": "rulings/ruling-20260403-cc.md", "name": "ruling-20260403-cc",
     "description": "owner ruling 2026-04-03: 索引重建拿不到鎖就讀舊快照，不要阻斷查詢",
     "body": "阻斷查詢比讀舊快照嚴重。"},
    {"path": "rulings/ruling-20260404-dd.md", "name": "ruling-20260404-dd",
     "description": "owner ruling 2026-04-04: 每晚整理批次只寫草稿，作廢與合併一律等我核准",
     "body": "附加欄位可以自動入卡，原話不准動。"},
    {"path": "grants/grant-20260405-ee.md", "name": "grant-20260405-ee",
     "description": "owner standing grant 2026-04-05: 發現缺陷直接修不必問，優先通案性修法",
     "body": "通案性修法的意思是同類缺陷一次修完。"},
    {"path": "corrections/correction-20260406-ff.md", "name": "correction-20260406-ff",
     "description": "owner correction 2026-04-06: 告警只給數字沒有下一步，我看不出要做什麼",
     "body": "數字要配一句「所以誰要做什麼」。"},
    # 鄰題干擾卡：詞面和上面的規則卡大量重疊，但答的是別的事。2026-09-05 事故
    # 的機制就是這種卡把答案卡擠出視窗，沒有它們，排序根本不必分辨任何東西。
    {"path": "notes/schedule-window-report.md", "name": "排程視窗週報",
     "description": "每週彙整排程任務的執行視窗與延遲分布；只報數字，不含處置方式",
     "body": "延遲分布用九成分位，不用平均值。"},
    {"path": "notes/schedule-rerun-log.md", "name": "排程重跑紀錄",
     "description": "記錄哪些排程任務被手動重跑過以及重跑的時間戳；不含重試策略",
     "body": "手動重跑要附一句原因。"},
    {"path": "notes/backup-retention-days.md", "name": "備份保留天數",
     "description": "完整備份保留三十天，增量備份保留七天；保留期滿自動輪替",
     "body": "輪替只刪過期的份，不動最近一份。"},
    {"path": "notes/backup-compression-ratio.md", "name": "備份壓縮比",
     "description": "備份壓縮比實測約三比一；壓縮在寫入前做，還原時自動解開",
     "body": "壓縮比隨資料表的重複程度變動。"},
    {"path": "notes/timestamp-precision.md", "name": "時間戳精度",
     "description": "時間戳存到毫秒；奈秒只在索引的新舊比較裡用，不進顯示層",
     "body": "毫秒對統計足夠，奈秒是為了避免同秒判斷不出先後。"},
    {"path": "notes/local-time-display-format.md", "name": "當地時間顯示格式",
     "description": "顯示層一律用年月日時分，不顯示時區縮寫，避免讀者誤解",
     "body": "需要時區時寫全名，不寫三個字母的縮寫。"},
    {"path": "notes/credential-rotation-calendar.md", "name": "憑證輪替日曆",
     "description": "金鑰與權杖每季輪替一次；輪替前後各留一週重疊期",
     "body": "重疊期結束要確認舊權杖真的沒有呼叫方在用。"},
    {"path": "notes/service-account-inventory.md", "name": "服務帳號盤點",
     "description": "列出每個服務帳號的用途與最後使用時間，未使用九十天就標註待收回",
     "body": "盤點只標註，收回是另一件要核准的事。"},
    {"path": "notes/cache-hit-ratio-report.md", "name": "快取命中率報表",
     "description": "每日輸出快取命中率與平均延遲；只是觀測，不含失效策略",
     "body": "命中率低於六成時值得看一眼鍵的設計。"},
    {"path": "notes/index-rebuild-duration.md", "name": "索引重建耗時",
     "description": "索引重建耗時隨卡片數線性成長；三百張卡約一秒",
     "body": "耗時的大頭是讀檔，不是寫資料庫。"},
    {"path": "notes/lock-contention-counters.md", "name": "鎖競爭計數",
     "description": "記錄拿不到鎖的次數與等待時間分布；只計數，不改任何行為",
     "body": "計數用來判斷要不要調整重建的頻率。"},
    {"path": "notes/console-colour-palette.md", "name": "主控台配色",
     "description": "主控台輸出只用兩種顏色，錯誤紅色其餘預設；非互動輸出時顏色關閉",
     "body": "顏色關閉的判斷看標準輸出是不是終端機。"},
    {"path": "notes/alert-channel-routing.md", "name": "告警通道路由",
     "description": "告警依嚴重度分流到不同通道；路由表與告警內容格式無關",
     "body": "最高嚴重度同時走兩個通道。"},
    {"path": "notes/observation-storage-quota.md", "name": "觀測資料配額",
     "description": "觀測資料的磁碟配額為十吉位元組，超過就先壓縮再輪替",
     "body": "配額用滿時先壓縮，壓縮完還是滿才輪替。"},
    {"path": "notes/traffic-shape-weekly.md", "name": "流量形狀週報",
     "description": "每週流量形狀與尖峰時段；是限流門檻的調整依據之一，本身不設門檻",
     "body": "尖峰時段近兩個月都落在同一個時窗。"},
    {"path": "notes/dependency-license-audit.md", "name": "相依授權稽核",
     "description": "列出每個相依套件的授權條款與相容性判斷；與版本釘不釘死無關",
     "body": "授權不相容的套件直接不列入候選。"},
    {"path": "notes/retry-budget-report.md", "name": "retry-budget-report",
     "description": "Weekly report of retry counts per job; observational only, it sets no cap and changes no behaviour.",
     "body": "The report exists so the cap can be argued with numbers."},
    {"path": "notes/writer-throughput-baseline.md", "name": "writer-throughput-baseline",
     "description": "Baseline write throughput per table, measured monthly for capacity planning.",
     "body": "The baseline is a capacity number, not a correctness property."},
    {"path": "notes/exit-code-inventory.md", "name": "exit-code-inventory",
     "description": "Catalogue of every exit code the jobs can return, with the meaning of each number.",
     "body": "The catalogue documents codes; it does not say what counts as success."},
    {"path": "notes/commit-size-distribution.md", "name": "commit-size-distribution",
     "description": "Distribution of changed lines per commit, used to size review batches.",
     "body": "The median commit is well under the split trigger."},
    {"path": "notes/test-runtime-budget.md", "name": "test-runtime-budget",
     "description": "The full test run must finish inside ten minutes; the slowest tests are listed here.",
     "body": "Runtime is a budget, not a statement about what the tests assert."},
    {"path": "notes/database-page-cache-tuning.md", "name": "database-page-cache-tuning",
     "description": "Page cache sizing for the local database; unrelated to writer concurrency.",
     "body": "The default sizing was measured to be adequate for this card count."},
    {"path": "notes/host-output-limits.md", "name": "host-output-limits",
     "description": "Observed host limits for injected output, measured once per client version.",
     "body": "Measured limits, not the budget the product chooses to spend."},
    {"path": "notes/timeout-histogram.md", "name": "timeout-histogram",
     "description": "Histogram of hook durations; this is the source of the timeout percentile numbers.",
     "body": "The tail is dominated by cold starts on a loaded machine."},
    {"path": "notes/model-pricing-notes.md", "name": "model-pricing-notes",
     "description": "Per-token prices for the models in use; a purchasing note, not a routing rule.",
     "body": "Prices are checked again whenever a model is added."},
    {"path": "notes/runner-image-versions.md", "name": "runner-image-versions",
     "description": "Which runner images are in use and when each of them was last refreshed.",
     "body": "An image refresh is announced before it lands."},
)

# 51 題：中文問中文卡、英文問英文卡、中文問別名卡（2026-09-05 事故形狀）、
# 中英混合、被取代卡不得出現、棄答，最後一組是刻意對不上詞面的難題。
SYNTHETIC_QUESTIONS = (
    {"id": "zh-retry", "prompt": "排程昨天晚上一直失敗，我看紀錄重跑了好幾次，到底要重試幾次才停下來",
     "expect": ["排程重試退避", "retry-cap-three"]},
    {"id": "zh-backup", "prompt": "資料表遷移之前要不要先備份", "expect": ["遷移前先備份"]},
    {"id": "zh-timezone", "prompt": "時間戳要存本地時間還是協調時間", "expect": ["時區一律存協調時間"]},
    {"id": "zh-secret-log", "prompt": "錯誤訊息裡不小心印出金鑰要怎麼處理",
     "expect": ["日誌不得寫入憑證", "mask-before-persist"]},
    {"id": "zh-cache", "prompt": "我明明寫進去了，讀出來還是舊值", "expect": ["寫入即失效快取"]},
    {"id": "zh-lock", "prompt": "索引重建拿不到鎖的時候查詢會不會被卡住",
     "expect": ["拿不到鎖就讀舊快照", "ruling-20260403-cc"]},
    {"id": "zh-encoding", "prompt": "繁體中文輸出在舊主控台就中斷了", "expect": ["主控台一律先轉編碼"]},
    {"id": "zh-alert", "prompt": "監控告警要寫什麼才不會被當成雜訊",
     "expect": ["告警必須帶下一步動作", "correction-20260406-ff"]},
    {"id": "zh-permission", "prompt": "這個服務帳號的權限應該開多大，批次跑完之後要不要收回", "expect": ["權限最小化"]},
    {"id": "zh-evidence", "prompt": "上次診斷留下的觀測資料現在還留著，磁碟快滿了，可以刪了嗎", "expect": ["稽核證據留到結案"]},
    {"id": "zh-ratelimit", "prompt": "限流要綁整體流量還是綁呼叫方", "expect": ["限流以呼叫方為單位"]},
    {"id": "zh-pin", "prompt": "相依套件的版本要釘死還是讓它浮動",
     "expect": ["相依版本必須釘死", "correction-20260402-bb"]},
    {"id": "en-stdlib", "prompt": "can I add a third-party dependency for this feature",
     "expect": ["prefer-stdlib-only"]},
    {"id": "en-idempotent", "prompt": "the retry duplicated rows in the table",
     "expect": ["idempotent-writers"]},
    {"id": "en-exitzero", "prompt": "exit code was zero but the payload came back empty",
     "expect": ["no-silent-skip"]},
    {"id": "en-pathspec", "prompt": "is it fine to stage everything for this commit",
     "expect": ["exact-pathspec-commits"]},
    {"id": "en-testing", "prompt": "the test only asserts that the command ran",
     "expect": ["test-behaviour-not-execution"]},
    {"id": "en-torn-index", "prompt": "two rebuilds ran at once and tore the database",
     "expect": ["single-writer-per-index"]},
    {"id": "en-truncated", "prompt": "the report was dropped by the host instead of shown, is there a size limit on it",
     "expect": ["bounded-output-budget"]},
    {"id": "en-timeout-gate", "prompt": "should the gate block the work when it times out",
     "expect": ["fail-open-on-timeout"]},
    {"id": "en-model-hook", "prompt": "can the hook call a model to rank the results",
     "expect": ["no-model-in-hot-path"]},
    {"id": "en-fixture", "prompt": "the fixture path never compares equal on the runner",
     "expect": ["resolve-fixture-roots"]},
    {"id": "en-one-fact", "prompt": "this record holds three separate claims at once",
     "expect": ["one-fact-per-record"]},
    {"id": "alias-mirror", "prompt": "我們的演練帳，有多少比例是正式帳會拒絕的，兩邊的標準是不是同一套",
     "expect": ["virtual-mirrors-live"]},
    {"id": "alias-restore", "prompt": "備份到底有沒有練過還原", "expect": ["rollback-drill-monthly"]},
    {"id": "alias-gate-quote", "prompt": "攔下來的時候要不要說是哪條裁定",
     "expect": ["gate-quotes-owner-words"]},
    {"id": "alias-measure", "prompt": "改之前改之後都要量嗎", "expect": ["measure-before-optimise"]},
    {"id": "alias-session", "prompt": "同一件事我在兩個地方問了", "expect": ["one-task-one-session"]},
    {"id": "alias-plain", "prompt": "報告不要堆專有名詞", "expect": ["plain-language-reports"]},
    {"id": "alias-cost", "prompt": "要跑長批次之前先講會吃多少資源",
     "expect": ["surface-cost-before-spending"]},
    {"id": "alias-two-fail", "prompt": "同一個做法失敗兩次還要再試第三輪嗎",
     "expect": ["two-failures-change-approach"]},
    {"id": "mix-hook-timeout", "prompt": "hook timeout 三秒到底夠不夠",
     "expect": ["hook-timeout-ten-seconds"], "forbid": ["hook-timeout-three-seconds"]},
    {"id": "mix-window", "prompt": "recall 視窗是五筆還是八筆",
     "expect": ["index-window-eight"], "forbid": ["index-window-five"]},
    {"id": "mix-nightly", "prompt": "nightly batch 每晚都還要我核准嗎",
     "expect": ["nightly-batch-standing-grant", "ruling-20260404-dd"],
     "forbid": ["nightly-batch-opt-in"]},
    {"id": "mix-scratch", "prompt": "暫存檔可以放專案第一層嗎", "expect": ["correction-20260401-aa"]},
    {"id": "mix-bugfix-grant", "prompt": "發現缺陷要先問你還是直接修",
     "expect": ["grant-20260405-ee"]},
    {"id": "sup-retry-cap", "prompt": "retry cap is how many attempts",
     "expect": ["retry-cap-three"], "forbid": ["retry-cap-five"]},
    {"id": "sup-nightly-approval", "prompt": "does the nightly batch need approval each night",
     "forbid": ["nightly-batch-opt-in"]},
    {"id": "abstain-espresso", "prompt": "espresso portafilter tamping technique",
     "expect_empty": True},
    {"id": "abstain-aquarium", "prompt": "熱帶魚缸的水草照明週期", "expect_empty": True},
    {"id": "abstain-falcon", "prompt": "遊隼的巢位在懸崖上", "expect_empty": True},
    # 難題組。前五題是 2026-09-05 事故的形狀：卡片只有英文（或只有中文）、沒有
    # 別名，問話換一個語言就對不上任何詞面。後五題是「詞面對得上但被鄰題擠掉」：
    # 干擾卡吃到兩三個切詞，答案卡只吃到一個。兩種缺口的修法不同，題庫要分得開。
    {"id": "hard-stdlib-zh", "prompt": "這個功能可以加第三方套件嗎",
     "expect": ["prefer-stdlib-only"]},
    {"id": "hard-idempotent-zh", "prompt": "同一個寫入器跑兩次會多出資料列",
     "expect": ["idempotent-writers"]},
    {"id": "hard-exitzero-zh", "prompt": "回傳零但產出的檔案是空的",
     "expect": ["no-silent-skip"]},
    {"id": "hard-testing-zh", "prompt": "這個測試只確認指令跑完了",
     "expect": ["test-behaviour-not-execution"]},
    {"id": "hard-retry-en", "prompt": "the scheduled job failed all night, how many retries before it stops",
     "expect": ["排程重試退避", "retry-cap-three"]},
    {"id": "hard-backup-en", "prompt": "do we take a full backup before a table migration",
     "expect": ["遷移前先備份"]},
    {"id": "hard-measure-noise", "prompt": "我改了快取的失效策略，改完要不要先量一次",
     "expect": ["measure-before-optimise"]},
    {"id": "hard-retry-noise", "prompt": "這個排程重跑很多次了，重試上限到底是幾次",
     "expect": ["排程重試退避", "retry-cap-three"]},
    {"id": "hard-alert-noise", "prompt": "告警的通道路由我改好了，內容還要寫什麼",
     "expect": ["告警必須帶下一步動作"]},
    {"id": "hard-permission-noise", "prompt": "服務帳號盤點完了，這個帳號的權限要開多大",
     "expect": ["權限最小化"]},
)


def write_synthetic_vault(root):
    for card in SYNTHETIC_CARDS:
        path = root / card["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_card_text(card), encoding="utf-8")
    return root


def _identifiers(card_path, name):
    posix = str(card_path).replace("\\", "/")
    stem = posix.rsplit("/", 1)[-1]
    return {
        posix.casefold(),
        stem.casefold(),
        stem[:-3].casefold() if stem.lower().endswith(".md") else stem.casefold(),
        (name or "").strip().casefold(),
    }


def _matches(result, token):
    return token.strip().casefold() in _identifiers(result["card_path"], result["name"])


def _rank_of(results, tokens):
    """1-based rank of the first result matching any token, else None."""
    for index, result in enumerate(results, 1):
        if any(_matches(result, token) for token in tokens):
            return index, result
    return None, None


def _card_evidence(vault, tokens, terms):
    """Which recall terms actually appear in the expected card's fields, read from
    disk: a card that no term touches is an aliasing gap, not a ranking bug."""
    for card_path, path, _mtime, _size in memsearch.scan_cards(vault):
        fields = memsearch._read_card(path)
        if not any(token.strip().casefold() in _identifiers(card_path, fields["name"]) for token in tokens):
            continue
        hits = {}
        for field in ("name", "description", memspec.ALIASES_FIELD, memspec.SCOPE_FIELD, "body"):
            value = (fields.get(field) or "").casefold()
            matched = [term for term in terms if term.casefold() in value]
            if matched:
                hits[field] = matched
        return card_path, hits
    return None, {}


def run_question(vault, question):
    prompt = question["prompt"]
    payload = memsearch.recall_index(vault, prompt, limit=DIAGNOSTIC_WINDOW)
    results = payload["results"]
    window = results[:RECALL_AT]
    terms = payload.get("terms") or []
    expect = [str(item) for item in question.get("expect") or []]
    forbid = [str(item) for item in question.get("forbid") or []]

    outcome = {
        "id": question.get("id") or prompt[:24],
        "prompt": prompt,
        "terms": terms,
        "expect": expect,
        "forbid": forbid,
        "expect_empty": bool(question.get("expect_empty")),
        "total": len(results),
        "top": [(item["card_path"], item["hit_fields"]) for item in window[:3]],
        "guidance": payload.get("guidance") or [],
    }

    if outcome["expect_empty"]:
        outcome["passed"] = not results
        outcome["reason"] = "" if outcome["passed"] else f"喚回了 {len(results)} 張卡，最前面是 {results[0]['card_path']}"
        return outcome

    leaked, _row = _rank_of(results, forbid) if forbid else (None, None)
    outcome["leaked_rank"] = leaked
    if expect:
        rank, row = _rank_of(results, expect)
        outcome["rank"] = rank
        outcome["hit_fields"] = row["hit_fields"] if row else []
        if rank is None:
            outcome["card_path"], outcome["term_hits"] = _card_evidence(vault, expect, terms)
    else:
        outcome["rank"] = None
    hit = (not expect) or (outcome["rank"] is not None and outcome["rank"] <= RECALL_AT)
    outcome["passed"] = hit and leaked is None
    # 失敗要分兩種，因為修法不同：排序缺口是候選裡有它、只是排太後面（改排序或
    # 改切詞有救），別名缺口是沒有任何切詞碰到那張卡（只有補別名有救，排序怎麼
    # 改都沒用）。把兩者混成一個數字，就無法判斷「量測不達標」該往哪邊修。
    if expect and not hit:
        outcome["gap"] = "ranking" if (outcome["rank"] or outcome.get("term_hits")) else "alias"
    else:
        outcome["gap"] = ""
    reasons = []
    if expect and not hit:
        reasons.append(f"期望卡排第 {outcome['rank'] or '—'} 名（門檻前 {RECALL_AT} 名）")
    if leaked is not None:
        reasons.append(f"被取代卡出現在第 {leaked} 名")
    outcome["reason"] = "；".join(reasons)
    return outcome


def run_corpus(questions, default_vault):
    outcomes = []
    built = set()
    for question in questions:
        vault = Path(question.get("vault") or default_vault).resolve()
        if vault not in built:
            memsearch.build_index(vault)
            built.add(vault)
        outcomes.append(run_question(vault, question))
    hits = sum(1 for item in outcomes if item["passed"])
    return {
        "outcomes": outcomes,
        "hits": hits,
        "total": len(outcomes),
        "recall": hits / len(outcomes) if outcomes else 0.0,
        "ranking_gaps": sum(1 for item in outcomes if item.get("gap") == "ranking"),
        "alias_gaps": sum(1 for item in outcomes if item.get("gap") == "alias"),
    }


def print_report(report, verbose=True):
    for outcome in report["outcomes"]:
        print(f"{'PASS' if outcome['passed'] else 'FAIL'} {outcome['id']}")
        if outcome["passed"] or not verbose:
            continue
        print(f"    prompt: {outcome['prompt']}")
        print(f"    {outcome['reason']}")
        print(f"    terms: {', '.join(outcome['terms']) or '(無)'}")
        if outcome.get("card_path") is not None or outcome.get("term_hits"):
            hits = outcome.get("term_hits") or {}
            detail = "; ".join(f"{field}={','.join(items)}" for field, items in hits.items())
            print(f"    期望卡 {outcome.get('card_path')} 命中欄位: {detail or '(這張卡沒有任何切詞命中)'}")
        elif outcome.get("hit_fields"):
            print(f"    期望卡命中欄位: {', '.join(outcome['hit_fields'])}")
        for index, (card_path, fields) in enumerate(outcome["top"], 1):
            print(f"    #{index} {card_path} hit_fields={','.join(fields)}")
    print(f"RECALL@{RECALL_AT} {report['hits']}/{report['total']}"
          f" ({report['recall'] * 100:.1f}%)")
    print(f"GAPS ranking={report['ranking_gaps']} alias={report['alias_gaps']}")


def load_corpus(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("題庫最外層必須是物件")
    questions = data.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValueError("題庫缺少 questions 清單")
    default_vault = data.get("vault")
    for index, question in enumerate(questions, 1):
        if not isinstance(question, dict) or not str(question.get("prompt", "")).strip():
            raise ValueError(f"第 {index} 題缺少 prompt")
        if not (question.get("expect") or question.get("forbid") or question.get("expect_empty")):
            raise ValueError(f"第 {index} 題沒有可機判的期望（expect／forbid／expect_empty）")
        if not (question.get("vault") or default_vault):
            raise ValueError(f"第 {index} 題沒有 vault，題庫也沒有預設 vault")
    return data


def _selftest():
    checks = []
    with tempfile.TemporaryDirectory(prefix="epitype-recall-regression-") as temp_dir:
        vault = write_synthetic_vault(Path(temp_dir).resolve())
        report = run_corpus(SYNTHETIC_QUESTIONS, vault)
        print_report(report)
        checks.append((
            f"合成題庫 RECALL@{RECALL_AT} {report['recall'] * 100:.1f}% 不低於門檻 {SYNTHETIC_RECALL_MIN * 100:.1f}%",
            report["recall"] >= SYNTHETIC_RECALL_MIN,
        ))
        checks.append((
            "題庫規模：卡片 ≥40 張、題目 ≥30 題",
            len(SYNTHETIC_CARDS) >= 40 and len(SYNTHETIC_QUESTIONS) >= 30,
        ))
        by_id = {item["id"]: item for item in report["outcomes"]}
        checks.append((
            "棄答題不得喚回任何卡",
            all(by_id[item["id"]]["passed"] for item in SYNTHETIC_QUESTIONS if item.get("expect_empty")),
        ))
        checks.append((
            "被取代卡不進候選視窗",
            all(by_id[item["id"]].get("leaked_rank") is None
                for item in SYNTHETIC_QUESTIONS if item.get("forbid")),
        ))
        # 被取代卡濾掉之後不能斷線。這句話只碰得到被取代卡的本文，後繼卡一個字
        # 都不match，所以「現行決定」那行指路是唯一的出口。
        guidance_probe = run_question(vault, {
            "id": "guidance", "prompt": "五筆的時代常常把答案卡擠到第六名",
            "forbid": ["index-window-five"],
        })
        checks.append((
            "被取代卡被濾掉時，guidance 指向後繼卡",
            guidance_probe["leaked_rank"] is None
            and any("index-window-eight" in line for line in guidance_probe["guidance"]),
        ))
        # 診斷本身也要被測：一題必失的問法要說出期望卡實際排第幾／命中哪些欄位。
        planted = run_question(vault, {
            "id": "diagnostic", "prompt": "退避倍數為二起始間隔十秒",
            "expect": ["rules/one-fact-per-record.md"],
        })
        checks.append((
            "診斷會說出期望卡沒進視窗，並印出它命中的欄位或「無切詞命中」",
            planted["passed"] is False and "rank" in planted and planted["card_path"] == "rules/one-fact-per-record.md",
        ))
        checks.append((
            "expect 可用卡名、檔名或 card_path 三種寫法指同一張卡",
            all(
                run_question(vault, {"id": token, "prompt": "資料表遷移之前要不要先備份",
                                     "expect": [token]})["rank"] is not None
                for token in ("遷移前先備份", "backup-before-migrate.md", "rules/backup-before-migrate.md")
            ),
        ))
        corpus_path = vault.parent / "corpus.json"
        corpus_path.write_text(json.dumps(
            {"vault": str(vault), "questions": [{"id": "q", "prompt": "權限要開多大", "expect": ["權限最小化"]}]},
            ensure_ascii=False), encoding="utf-8")
        loaded = load_corpus(corpus_path)
        checks.append(("題庫載入器讀得回 vault 與題目", loaded["vault"] == str(vault) and len(loaded["questions"]) == 1))
        bad_path = vault.parent / "bad.json"
        bad_path.write_text(json.dumps({"vault": str(vault), "questions": [{"prompt": "沒有期望"}]},
                                       ensure_ascii=False), encoding="utf-8")
        try:
            load_corpus(bad_path)
            rejected = False
        except ValueError:
            rejected = True
        checks.append(("題庫載入器拒絕沒有可機判期望的題目", rejected))

    passed = sum(1 for _label, ok in checks if ok)
    total = len(checks)
    for label, ok in checks:
        if not ok:
            print(f"FAILED: {label}")
    print(f"SELFTEST {'PASS' if passed == total else 'FAIL'} {passed}/{total}")
    return 0 if passed == total else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="喚回品質回歸測試。")
    parser.add_argument("corpus", nargs="?", help="題庫 JSON 路徑；省略時需給 --selftest")
    parser.add_argument("--selftest", action="store_true", help="跑內建合成 vault 與題庫")
    parser.add_argument("--strict", action="store_true", help="未達門檻時 exit 1")
    parser.add_argument("--quiet", action="store_true", help="只印分數，不印每題診斷")
    options = parser.parse_args(argv)

    if options.selftest:
        return _selftest()
    if not options.corpus:
        parser.error("需要題庫路徑或 --selftest")
    data = load_corpus(options.corpus)
    report = run_corpus(data["questions"], data.get("vault"))
    print_report(report, verbose=not options.quiet)
    threshold = data.get("min_recall")
    if threshold is not None:
        print(f"THRESHOLD {float(threshold) * 100:.1f}%")
    if options.strict and threshold is not None and report["recall"] < float(threshold):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
