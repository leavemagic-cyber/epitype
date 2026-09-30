# Codex V2 生命週期查證（I，2026-10-01）

本項只研究；未設定 Codex 同時上限、未實作不存在的 close 工具。

官方版本 `rust-v0.156.0` 的 [V1 工具定義](https://github.com/openai/codex/blob/rust-v0.156.0/codex-rs/core/src/tools/handlers/multi_agents_spec.rs) 包含 `close_agent`；[V2 handlers](https://github.com/openai/codex/blob/rust-v0.156.0/codex-rs/core/src/tools/handlers/multi_agents_v2.rs) 註冊 followup_task、interrupt_agent、list_agents、send_message、spawn_agent、wait_agent，沒有 close_agent。interrupt 不是關閉。

[狀態原始碼](https://github.com/openai/codex/blob/rust-v0.156.0/codex-rs/core/src/agent/status.rs) 分開 Completed 和 Shutdown。完成一回合不等於立即關閉執行緒。

[residency.rs:117–159、224–237](https://github.com/openai/codex/blob/rust-v0.156.0/codex-rs/core/src/agent/control/residency.rs#L117) 顯示：容量不足時嘗試卸載 LRU 候選；僅 V2 子代理且 Completed／Errored／Interrupted、沒有 active turn 和 pending mailbox 才符合卸載條件。流程保存 rollout、shutdown_and_wait、保存 environment，再 remove_thread。這支持「容量壓力下可回收閒置完成代理」，不支持「一完成即釋放」。

[config/mod.rs:252、2713 附近](https://github.com/openai/codex/blob/rust-v0.156.0/codex-rs/core/src/config/mod.rs#L252) 的 V2 **內部**預設數字是 4；公開 agents.max_concurrent_threads_per_session 映射到內部值時加 1。從該映射推論內部計入 root，不能把內部 4 當成使用者可開 4 個子代理。功能覆寫也能先於這項設定。

本機實查 CLI 是 0.159.1，桌面 App 的引擎版本未與上述 tag 對齊。官方 [subagents 文件](https://developers.openai.com/codex/subagents) 與上述固定版本源碼不能證明本機 App 現場名額釋放。#39694 是使用者問題回報，不是維護者確認；本批沒有重現。當前 V2 工具沒有 close，因此「做完要關」在此介面仍無可用關閉操作。

交 owner 的決定點：是否接受 V2 的容量回收生命週期，或改用有官方關閉介面的工作方式。未決定前不新增 V2 關閉關卡、不設上限。驗證現場釋放還需明確授權的代理容量實驗；本批沒有為研究另開代理。
