# 宿主交接（2026-10-01 實查）

共同程序：保存可讀交接檔 → 開接手場 → 新主控讀正本、核對檔案版本並確認接手 → 舊場才停止。傳訊成功、工具說已建立都不算接手。

Claude：[Desktop](https://code.claude.com/docs/en/desktop#manage-sessions) 明確說一般 Code 對話可列場、傳訊，建議新工作時出 task chip，由使用者點擊開新場；[跨場訊息](https://code.claude.com/docs/en/cross-session-messaging) 有平台／版本與入站限制。本機 Windows 的門檻是 2.1.234；不能把 macOS 的可用狀況當成本機已可用。Dispatch／Projects 是另外的產品表面，不推導成一般 Code 對話自行開場。

Codex：本場官方工具表可見 create_thread、send_message_to_thread、handoff_thread，但公開文件／公開原始碼中未找到足以保證這些桌面插件工具語意及 hook 覆蓋的支援契約。[#44707](https://github.com/openai/codex/issues/44707) 是使用者提交到官方 repo 的問題回報，不能當成維護者已確認；其聲稱的繞過也不以修改 hooks matcher 解決。doctor 標自動路徑 UNVERIFIED。保留 owner 開新場、檔案交接的可用路徑，不呼叫未授權建場。

`python -m epitype.lead_transfer prepare <交接檔> --session <原場id>` 保存 SHA-256；接手者讀檔後用 `accept`、不同 `--session` 與 `--sha256` 確認；`check` 只回 `ready_to_stop`。交接後再改檔會撤銷 ready。此狀態不是 owner 的授權，也不殺程序。測試 1/1，包含未接手、同場、錯版本與接手後改檔。
