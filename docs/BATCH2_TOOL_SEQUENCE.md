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

七天真實重放與目標身分驗收未完成前，不把「讀對檔、查主樹、关對分頁」的產品缺口轉成已強制。原有 gap 保留。這些新欄位不能降低原本要求。
