# 模組與介面

## 選擇

兩個獨立架構候選都建議「目前狀態加追加歷史」。採集中 Store，避免第一版引入完整事件投影與投影版本。保留候選 B 的重試識別與事件時間，支援歷史查核。

## 模組責任

- `monitorbot/store.py`：SQLite、任務狀態、知識、問題、偏好、來源健康與操作歷史。唯一資料寫入入口。
- `monitorbot/sources.py`：來源輪詢與原文回查。內文只在記憶體流動。
- `monitorbot/agents.py`：非互動 CLI、結構化結果、逾時與對話不落盤。
- `monitorbot/reports.py`：週報與系統檢查檔案。
- `monitorbot/config.py`：本機設定與預設值。
- `monitorbot/service.py`：輪詢、獨立複核、排程與報告協調。
- `monitorbot/desktop.py`：Mac 每日互動與清單。
- `monitorbot/improve.py`：隔離變更、驗證、審查與回復。
- `monitorbot/__main__.py`：固定 Python 指令介面。

## 平行實作契約

所有程式相容 Python 3.9，執行時不新增第三方相依。設定採字典，時間採含時區的 ISO 8601 字串，資料目錄由呼叫者傳入。

`sources.collect(config, cursors, since, now)` 回傳 `SourceBatch` 清單，每筆有 `source`、`events`、`status`、`detail`、`cursor`。status 為 `ok`、`partial`、`error`、`unconfigured`、`excluded` 之一。事件有 `source`、`source_id`、`occurred_at`、`title`、`url`、`metadata`，可額外有暫存的 `content`。`sources.read_content(event, config)` 依索引回查內文。持久化白名單由 Store 負責，不能直接保存整筆來源輸出。

`agents.AgentRunner(config, data_dir).run(role, payload, schema)` 回傳 JSON 字典。每次呼叫獨立 CLI 上下文。報告模組使用同一介面，不取得資料寫入權限。

Store 的具體方法由資料核心實作者先固定並通知主 agent，其他模組不得直接連 SQLite。CLI 模型只能回傳提案，由服務呼叫 Store 驗證及套用。

## 驗證

先用暫存資料與合成內容驗證狀態規則與 CLI，真實來源依實際權限接通。失敗時保留最後成功游標，重新回查未完成活動。測試資料不得混入正式紀錄或週報。
