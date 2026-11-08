---
name: monitorbot
description: 使用本專案的固定 Python CLI 記錄工作任務與靈感、查詢待辦與工作知識、提出有證據的狀態變更，以及產生週報。適用於使用者的 monitor bot 工作記憶資料，不用於一般程式碼監控或系統效能監控。
---

# Monitor bot

從包含 `monitorbot/` 的專案根目錄執行 `python3 -m monitorbot`。資料目錄依序取 `--home`、`MONITOR_HOME`，或專案內的 `.monitor`。`--home` 放在子命令之前，同一項工作使用同一個資料目錄。

從全域 skill 入口使用時，先解析本 `SKILL.md` 的符號連結，其所在資料夾向上兩層即為執行根目錄。部署後的 CLI 會沿本機 manifest 使用已安裝版本與同一份資料。

先依需要查閱 [CLI 與啟動方式](../../README.md)。來源能力與限制見 [來源文件](../../docs/sources.md)，責任與資料邊界見 [架構](../../docs/architecture.md)。未經要求不要安裝全域 skill 或切換資料目錄，資料讀取遵循目前的來源範圍設定。

## 查詢與手動記錄

使用者明確要記錄一句靈感時，直接保存，不等待模型分析。

```bash
python3 -m monitorbot add '有空研究 HDR' --kind optional
python3 -m monitorbot task list
python3 -m monitorbot task get TASK_ID
python3 -m monitorbot task history TASK_ID
python3 -m monitorbot knowledge list --topic HDR
python3 -m monitorbot ask '我們現在 HDR 怎麼做，支援哪些機制？'
```

`add` 預設為 `optional`。只有使用者明確承諾或接受指派時才用 `--kind assigned`。`--due-at` 必須有明確期限依據，使用含時區的 ISO 8601，不把預估可開始時間填成交付期限。

回答現況時區分提案、決定、實作與驗證，保留適用產品、平台、版本及時間。列出來源索引與未解衝突，不能把找到關鍵字當成機制已支援。`ask` 回傳知識整理，不代表已驗證全部來源可讀取。

## 提出任務與知識變更

外部 agent 經提案入口操作，由服務執行獨立複核。先查現有任務與知識，再取得當前格式。

```bash
python3 -m monitorbot schema
python3 -m monitorbot propose --json @plan.json
```

也可用 `propose --json -` 從 stdin 提供 JSON。`plan.json` 只保存整理後的提案與來源識別，不放郵件、聊天、文件原文或憑證。不要複製 schema 到自己的規則檔，實際欄位以 `schema` 為準。

提案包含 `tasks`、`knowledge`、`questions`。不需要變更的類別使用空陣列。新紀錄的 `id` 為 `null`，既有紀錄使用查到的 ID。證據 ID 必須已入庫且未被排除，不能編造來源或紀錄 ID。缺少來源時先用 `doctor` 檢查涵蓋範圍。

只將實際新增要求、進展、產出、期限或責任變化標為 `substantive`。重讀、摘要與產生報告不算實質更新。

- `waiting` 表示使用者仍須接手，前置工作尚未完成。保留等待對象與預估時間，等待兩個月也不能當成閒置。
- `candidate` 表示尚未明確由使用者負責，`optional` 包含有空研究與靈感。無承諾、無期限且長期無進展時，可由服務判定是否註銷。
- 請使用者找人拿資料或協助，不等於移交使用者的交付責任。
- 工作知識跨任務累積。新的結論保留來源、適用範圍與取代關係，不因 task 完成而刪除知識。

不要直接修改 SQLite，或用 `task update`、`task transition`、`--reviewed`、`--actor user` 繞過提案與獨立複核。使用者沒有回答的確認題，不能代替使用者選建議答案或建立長期偏好。

## 週報與失敗處理

```bash
python3 -m monitorbot report
python3 -m monitorbot doctor
```

每週一排程自動整理上一個完整週。使用者要求即時整理時才執行 `report`，依回傳路徑交付檔案。成果按事件發生時間歸屬，目前待辦另標示整理時點。遇到衝突由追蹤 agent 調整後繼續，未解衝突不能當成已完成報告。

CLI 登入失效、用量限制、來源未接通或回查失敗時，回報受影響範圍，不用空資料宣稱沒有工作，也不反覆執行相同失敗。來源缺漏與內容超限不能作為完成或閒置註銷的依據。

原始雲端內容只供當次分析，不另存檔、不寫入操作紀錄，也不將原文貼入提案。後端必須通過本工具的 synthetic 持久化查核才能分析原文。知識、索引與判斷歷史預設永久保存；本 skill 不授權清理、刪除來源資料或自行修改系統。
