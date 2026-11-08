# CLI agent 執行與報告介面

## 執行設定

`AgentRunner(config, data_dir).run(role, payload, schema)` 使用獨立的非互動 CLI 工作階段，回傳通過結構驗證的 JSON 物件。`payload` 只經 stdin 傳入，不寫提示檔、CLI 原始輸出或 stderr 日誌。

```json
{
  "agents": {
    "default": {"backend": "codex", "timeout": 180},
    "roles": {
      "reviewer": {"backend": "claude", "model": "使用者指定的模型"}
    }
  }
}
```

後端為 `codex` 或 `claude`。每個角色可覆寫 `backend`、`model`、`executable`、`timeout` 與 `max_input_chars`。沒有指定模型就使用 CLI 的預設模型，不猜測訂閱提供的模型名稱。初始化設定最多等待 240 秒，逾時終止子程序群組。

程式沿用 CLI 登入，不傳送環境中的模型 API Key，也不自動改成 API Key 認證。CLI 不存在、登入失效、額度不足或輸出格式錯誤都會失敗，錯誤訊息不包含輸入與原始輸出。

Codex 使用 `exec`、`--ephemeral`、`--ignore-user-config`、唯讀 sandbox 與 JSON Schema。工具、插件、hooks 與記憶功能停用。忽略使用者設定後仍沿用登入，但自訂模型供應商或網路設定可能需要另外驗證。參考 [OpenAI 非互動模式](https://learn.chatgpt.com/docs/non-interactive-mode)。

Claude 使用 `-p`、`--no-session-persistence`、`--safe-mode`、空工具集與嚴格 MCP 設定，不使用要求 API Key 的 `--bare`。參考 [Claude CLI 文件](https://code.claude.com/docs/en/cli-reference)。本機說明已確認上述旗標存在，實際登入與背景 Keychain 存取仍須測試。

## 原文留存查核

`runner.smoke_test()` 會呼叫一次真實模型，使用隨機合成標記，不讀取真實來源。呼叫前後比較已知 CLI session 與 log 位置的檔案資訊，只讀取變動檔案來尋找標記，不讀憑證或未變動的舊對話。

結果包含 `privacy_verified`、後端執行檔的 `fingerprint`、檢查範圍、發現標記的位置及未能檢查的檔案。大型檔案以串流掃描，發現持久標記或檔案無法讀取時不通過。這項查核只涵蓋已知位置，不能證明第三方 CLI 沒有任何未知的遙測或記錄路徑。

成功結果保存於 `agents.privacy_verified[backend]`。`run()` 遇到具有來源與內文的資料時，要求該後端已通過查核，且 `fingerprint(role)` 仍一致。CLI 更新或執行檔改變後須重新查核。合成測試不含真實來源，不提供可略過檢查的參數。

單元測試使用程序替身，驗證 stdin、旗標、格式、逾時與錯誤保密。單元測試通過不代表真實模型、登入或原文留存查核通過。

## 報告

`previous_week(now, timezone)` 回傳上一個完整週的起訖時間，起點包含、終點不包含。預設時區為 Asia/Taipei。

`generate_weekly(runner, data, start, end, generated_at)` 與 `generate_system_report(...)` 各自呼叫獨立 reporter 工作階段。`data` 可包含 `events`、`history`、`tasks`、`knowledge`、`health` 與 `logs`。回傳 `markdown` 和 `conflicts`。每個衝突包含 `task_id` 與 `message`，由服務交回追蹤 agent 處理。

週報依事件時間整理成果，當前任務另標整理時點。歷史完成與目前重開的任務都交給 reporter，不能先用目前狀態抹掉歷史。系統報告區分提出改善與已驗證修正。

`save_report(data_dir, kind, start, end, result)` 只接受 `weekly` 或 `system`。有未解衝突或內容空白就不發布。同期間相同內容回傳既有檔案，內容變更產生新版本，檔名含期間與內容雜湊。寫入使用暫存檔與原子替換，保留舊版本。
