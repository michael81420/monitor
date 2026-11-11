# Monitor bot

本機背景工作記憶工具。它記錄活動索引，追蹤任務，累積工作知識，每週一整理上一週的 Markdown 週報。

程式使用 Python 3.9 以上版本與標準庫。模型透過已登入的 Codex CLI 或 Claude Code 執行，不另外要求模型 API Key。來源資料仍需要自己的讀取權限。

## 啟動

在本專案目錄執行：

```sh
python3 -m monitorbot init
python3 -m monitorbot doctor
python3 -m monitorbot add "有空研究 HDR 測試方式"
python3 -m monitorbot task list
```

預設資料放在專案下的 `.monitor`。所有命令都可以在命令名稱前加 `--home PATH`，或透過 `MONITOR_HOME` 指定資料目錄。

```sh
python3 -m monitorbot --home /private/tmp/monitor-demo init --lookback-days 7
```

回溯天數預設為 0。初始化時記錄開始時間，重跑初始化不重設時間。0 天表示不批次匯入歷史，需要理解新任務時仍可依來源索引回查舊內容。

`add` 立即保存輸入與任務，不等待模型。預設是可閒置註銷的 `optional`，已承諾的工作可明確指定：

```sh
python3 -m monitorbot add "完成驗證報告" --kind assigned --project Display --due-at 2026-12-01T17:00:00+08:00
```

## CLI agent 驗證

先在選用的 CLI 完成訂閱帳號登入，再執行合成測試：

```sh
python3 -m monitorbot agent-check --backend codex
```

也支援 `--backend claude`。這個命令會呼叫一次模型，使用 CLI 額度。它不讀真實信件或聊天，會檢查已知 CLI 對話與日誌位置是否另存合成內容。只有通過且執行檔指紋仍相符的後端，才能處理來源正文。

這項檢查不保證涵蓋 CLI 未知的儲存位置。CLI 更新後需要重跑。登入過期、訂閱額度或 macOS Keychain 限制也可能中斷背景執行。

切換預設後端：

```sh
python3 -m monitorbot config set --json '{"agents":{"default":{"backend":"claude"}}}'
python3 -m monitorbot agent-check --backend claude
```

各角色可在 `agents.roles` 個別設定 `backend`、`model`、`executable` 與 `timeout`。主要角色為 `tracker`、`reviewer`、`reporter`。模型名稱不預先綁死，省略時使用該 CLI 的預設模型。

## 來源與實際涵蓋範圍

已實作擷取路徑不代表這台電腦已完成連接。`doctor` 顯示現有來源健康狀態，`poll` 才會實際嘗試讀取。

| 來源 | 擷取方式 | 限制與設定 |
|---|---|---|
| Chrome | 唯讀查詢 profile 的瀏覽歷史 | 保存時間、標題與網址，不記錄無痕活動，不取得已登入網頁的完整正文 |
| Word | macOS Spotlight 最近使用資訊，或指定路徑 | 讀取 `.docx` 的文字；不監看按鍵與未儲存草稿 |
| Excel | 同上 | 讀取 `.xlsx` 已儲存的資料，不保證圖表、巨集與尚未計算的值 |
| PowerPoint | 同上 | 讀取 `.pptx` 文字，不保證圖片與版面完整還原 |
| Codex | 現有 CLI JSONL 對話索引 | 不另複製逐字稿，暫存讀取訊息內容；ephemeral 對話沒有可回溯檔案 |
| Claude | 現有 Claude Code JSONL 對話索引 | 同上，不涵蓋所有工具輸出 |
| ChatGPT | 來源狀態可辨識 | 尚無直接對話讀取連接，不能宣稱已記錄完整 ChatGPT 對話 |
| Outlook | Microsoft Graph 委派授權 | 需設定應用程式 client ID、完成登入，可能需要組織管理員同意 |
| Teams | Microsoft Graph 委派授權 | 同上，涵蓋可授權存取的聊天與頻道，不保證所有組織內容 |
| Gerrit | HTTPS REST API，匿名或帳號 HTTP token | 指定 query，收集 change 更新、說明及已發布 review 留言，不包含 diff 與草稿 |

Gerrit 設定與權限範圍見 [來源設定](docs/sources.md#gerrit)。沒有安裝原生 Office 也能讀取指定的本機 Office 文件，但不會因此取得 Office 網頁版中的全部操作。

每種來源各有設定。例如明確指定 Office 文件範圍：

```sh
python3 -m monitorbot config set --json '{"sources":{"excel":{"paths":["/Users/your-name/Work"],"recent":false}}}'
```

Chrome 可設定 `sources.chrome.profiles`，內容為 profile 目錄路徑。Codex 與 Claude 可分別設定 `sources.codex.roots`、`sources.claude.roots`。已設定路徑不代表授予 macOS 檔案權限，讀取被拒絕時來源會顯示錯誤。

### Microsoft 登入

Microsoft `client_id` 是已註冊公用用戶端應用程式的識別，不是模型 API Key，也不是 client secret。應用程式須允許 device-code 委派登入，並具備需要的 Microsoft Graph 讀取權限。

```sh
python3 -m monitorbot config set --json '{"microsoft":{"client_id":"YOUR-REGISTERED-CLIENT-ID","tenant":"organizations"}}'
python3 -m monitorbot source microsoft login-start
```

依輸出網址與 user code 完成登入後，等待輸出指定的間隔，再執行一次：

```sh
python3 -m monitorbot source microsoft login-poll
```

尚未完成時命令回傳 `authorization_pending` 與建議等待秒數，不會無限等待或自行開瀏覽器。需等候時稍後再執行 `login-poll`。成功後憑證保存在本工具資料目錄的權限 0600 檔案。`doctor` 只檢查檔案是否存在與權限，不讀取 token 內容。

## 範圍與過濾

每個來源都有 `enabled`、`include` 與 `exclude`。規則使用欄位與 glob，比對不分英文大小寫。同一條規則內的欄位全部符合才命中，多條規則任一命中即可。排除規則優先。

```sh
python3 -m monitorbot filter add outlook exclude --json '{"sender":"*@newsletter.example"}'
python3 -m monitorbot filter add teams include --json '{"channel_id":"YOUR-WORK-CHANNEL-ID"}'
python3 -m monitorbot filter preview outlook
python3 -m monitorbot filter remove outlook exclude 1
```

新增、移除或修改來源設定時，既有索引立即重新套用範圍。任務所有有效證據都被排除時退出待辦，歷史與知識保留。有其他未排除來源則持續追蹤。解除來源排除不會單憑舊證據自動恢復任務。

## 每日確認與待辦

預設每小時分析新增內容。每日清單與確認預設週一至週五 09:30 顯示，工作時間至 18:00。問題基準 10 題，積欠時可到 20 題，顯示前先用最新資料重查。沒有疑點就不跳詢問視窗。

```sh
python3 -m monitorbot review
python3 -m monitorbot question list
python3 -m monitorbot question answer QUESTION-ID --choice 2
python3 -m monitorbot question answer QUESTION-ID --text "其他回應"
```

`--choice` 從 1 起算。加入 `--always` 可將答案套用至已定義範圍的同類問題。單次回答不直接改任務，後續交由追蹤與複核流程處理。

調整頻率與題數：

```sh
python3 -m monitorbot config set --json '{"schedule":{"days":[0,1,2,3,4,5,6],"daily_time":"10:00","question_limit":10,"backlog_limit":20}}'
```

星期一為 0，星期日為 6。待辦區分輪到使用者與等待前置工作，顯示各自的超時。已承諾的工作及等待兩個月後接手的工作不因 30 天無進展而註銷。

## 查詢與週報

```sh
python3 -m monitorbot ask "目前 HDR 怎麼做，支援哪些機制？"
python3 -m monitorbot knowledge list --topic HDR
python3 -m monitorbot knowledge list --project Display
python3 -m monitorbot task history TASK-ID
python3 -m monitorbot report
```

查詢先縮小本機候選，再按需要回查來源。知識分為 `proposed`、`decided`、`implemented` 與 `verified`，保留版本、適用範圍與來源，不把提案當成已實作。

每週一 09:30 自動整理上一個完整週，時區預設 `Asia/Taipei`。`report` 可立即要求整理上一週，先更新追蹤，再啟動獨立報告 agent。週報與系統檢查報告分開放在 `reports/weekly` 與 `reports/system`。每次更正產生內容識別的檔案版本，保留舊版本。

工作週報依事件發生時間歸屬成果，另外列整理當下的待辦。系統檢查報告供改善流程查找來源缺口、誤判與重複問題，區分已發現、處理中與已驗證修正。單憑 agent 同意不代表修正已驗證。

## 背景執行

Mac 上方選單列提供清單圖示與輪到你處理的數量。選單先顯示待辦、等待、逾期、待確認、背景排程是否啟用及最後收集檢查時間，來源異常與完整任務細節可展開查看。讀取清單的時間放在來源詳情，不代表資料剛完成收集。點開可分別查看「輪到你處理」與「等待他人」，順序沿用追蹤系統的優先程度。點任務可看下一步與期限，逾期與前置工作超時會標示。

選單也提供新增一句話待辦、每日確認與完整清單、重新整理。清單每分鐘同步，點開時重新讀取，不呼叫模型。關閉圖示不會停止背景收集，可用 `python3 -m monitorbot menubar start` 重新開啟。

先手動驗證一次，避免將未處理的登入或權限問題藏在背景：

```sh
python3 -m monitorbot poll
python3 -m monitorbot deploy
python3 -m monitorbot start --login
python3 -m monitorbot status
```

macOS 對 Desktop 的背景存取限制可能讓 launchd 無法載入模組。`deploy` 將執行副本與資料放到使用者的 `Library/Application Support/MonitorBot`，原專案 `.monitor` 保留指向同一份資料的符號連結。CLI 依 manifest 使用已部署版本，重複部署從原專案更新並保留舊程式版本。自動改善修改的是已部署版本。

`deploy` 準備新的排程但不啟動。`start --login` 載入排程，並在 `~/Library/LaunchAgents` 建立本工具的登入啟動連結。每分鐘執行一次 `tick`，由資料庫中的時間決定是否需要輪詢、顯示每日清單或產生週報。`install` 只準備目前位置的 plist，供不需要部署的環境使用。

背景程式不將 agent 的 stdout 或 stderr 寫入日誌檔。外層啟動錯誤寫入資料目錄的 `service-errors.log`，必要的運作與判斷紀錄保存在資料庫。電腦休眠或關機時暫停，恢復後從最後成功位置補查。來源失敗時不前進成功游標，也不能據此閒置註銷。

```sh
python3 -m monitorbot stop --login
```

`stop --login` 停止目前排程並移除本工具的登入啟動連結，不刪資料。單獨 `stop` 只停止目前排程。也可用 `run --loop` 在前景每分鐘檢查排程，按 Ctrl-C 停止；`run --once` 與 `poll` 都只執行一次收集分析。

單獨管理選單列圖示：

```sh
python3 -m monitorbot menubar status
python3 -m monitorbot menubar stop
python3 -m monitorbot menubar start
```

首次啟動圖示需要本機 Swift 編譯器，使用系統 AppKit，不安裝額外 Python 套件。編譯快取位於暫存目錄，完成後清除。

## 固定資料操作介面

需要完整欄位時，使用 JSON。`--json` 支援直接字串、`-` 讀 stdin，以及 `@file` 讀檔。

```sh
python3 -m monitorbot task update TASK-ID --json '{"next_action":"等待開發完成後進行 IT 驗證","status":"waiting","waiting_for":"開發負責人","waiting_until":"2027-01-08T09:00:00+08:00"}'
python3 -m monitorbot task transition TASK-ID completed --reason "已確認交付"
python3 -m monitorbot knowledge upsert --json '{"topic":"HDR","content":"已採用的機制摘要","stage":"implemented","project":"Display"}'
```

這些命令預設代表使用者明確操作。agent 應透過 `propose` 提交計畫，由服務驗證與啟動獨立複核：

```sh
python3 -m monitorbot propose --json @plan.json
python3 -m monitorbot schema
```

計畫格式由 `service.TRACK_SCHEMA` 定義，包含 `tasks`、`knowledge` 與 `questions`。agent 不得自行宣稱已完成獨立複核，也不應使用使用者身分繞過服務。服務檢查來源與 revision，讀取後資料已變動時拒絕過時判斷。

正常結果以 JSON 輸出。錯誤以 JSON 寫入 stderr，結束碼為 2，不輸出 traceback。

## 保存與清理

本機保存任務、整理後的知識、來源索引、判斷與變更歷史及報告。雲端郵件、聊天與文件原文不另存副本。永久保存是預設，清理排程預設停用。

先預覽，再使用同一份預覽 token 執行：

```sh
python3 -m monitorbot cleanup preview --before 2026-01-01T00:00:00+08:00 --types logs events
python3 -m monitorbot cleanup apply --before 2026-01-01T00:00:00+08:00 --types logs events --confirmation PREVIEW-TOKEN
```

第一版指令清理範圍是資料庫的 `logs`、未被引用的 `events`、已結束的 `questions`、`history` 與 `knowledge_history`。活躍任務、目前知識與仍被保留資料引用的證據受保護。預覽後候選有變動，舊 token 會被拒絕。

這個指令不刪報告檔、原始文件、郵件、聊天或既有 CLI 對話。永久保存仍會增加磁碟用量。來源刪除或權限失效後，只有索引與整理過的知識，不保證能重現當時原文。

需要定期清理時，可明確設定保留天數與類型。以下範例每 30 天清理超過 180 天的運作紀錄，會實際刪除符合政策的資料；預設沒有啟用此設定。

```sh
python3 -m monitorbot config set --json '{"cleanup":{"enabled":true,"interval_days":30,"policy":{"retain_days":180,"types":["logs"]}}}'
```

## 驗證與設計

```sh
python3 -m unittest discover -s tests -v
```

測試使用暫存資料與合成內容，不登入 Microsoft、不呼叫真實模型，也不啟動 LaunchAgent。實際來源、訂閱登入、macOS 權限與 CLI 原文持久化需另外驗證。

可重跑實際 OS 隔離與合成端到端驗證：

```sh
python3 scripts/verify_runtime.py --isolation
python3 scripts/verify_runtime.py --workflow
```

第一個指令在臨時副本執行基準、檢查外部讀寫與連網被拒絕，並由父程序確認 SQLite 資料保留。第二個指令會使用現有 CLI 登入呼叫真實模型，但只提供合成任務，不將測試資料放進正式資料庫。已部署時以安裝中的版本為準。

- [設計決策](docs/monitor-bot-design.md)
- [模組介面](docs/architecture.md)
- [工作用語](CONTEXT.md)
- [實際驗證紀錄](docs/verification.md)

## Windows 背景執行與驗證

Windows 使用目前使用者的 Task Scheduler，在登入的互動工作階段執行既有排程迴圈。請將專案放在固定位置，安裝 Python 3.9 以上含 tkinter，並讓 Codex 或 Claude CLI 可由該使用者啟動。Windows 通常需要另外安裝時區資料。

```powershell
py -m pip install tzdata
py -m monitorbot init
py -m monitorbot doctor
py -m monitorbot install
py -m monitorbot start --login
py -m monitorbot status
py -m monitorbot review
py -m monitorbot stop --login
```

`install` 只產生排程定義。`start` 註冊並啟動，`--login` 加入登入啟動。`stop` 停止這次執行，`stop --login` 同時移除本工具排程，不刪資料。`deploy` 是 macOS 專用搬移流程，Windows 直接從固定專案目錄執行。Windows 每日確認使用 tkinter；選單列 app 仍是 macOS 專用。

Windows Office 來源需明確設定各來源的 `paths`，例如 `C:/Work`；不使用 macOS Spotlight。Chrome 預設讀取 `%LOCALAPPDATA%/Google/Chrome/User Data`。既有檔案及 API 來源共用同一套篩選、任務與週報流程。

可先在任何平台執行合成測試，再到 Windows 驗證真實排程。

```powershell
py -m unittest tests.test_windows tests.test_gerrit -q
py scripts/verify_windows.py --help
```

實機工具的命令與限制見 [Windows 驗證](docs/windows.md)。Windows ACL 沿用資料目錄的使用者權限；POSIX `0600` 並不等於 Windows ACL，請使用自己的私人資料目錄。

## 使用合成資料預覽 Mac 工具列

這個預覽只讀取範例快照，停用新增與每日確認，不連線來源或呼叫模型。需要 macOS 與 Swift 編譯器。在專案目錄執行：

```sh
mkdir -p .build
swiftc -O -framework Cocoa -module-cache-path .build/ModuleCache native/MenuBar.swift -o .build/MonitorPreview
.build/MonitorPreview --snapshot tests/fixtures/menubar.json
```

點選新出現、顯示 6 項待辦的圖示。範例任務皆標示「合成預覽」，包含逾期、等待超時、待確認與來源尚未連接的狀態。完成後在預覽選單選擇「關閉圖示」。這不會停止原本的背景收集。

若只需檢查選單階層，在最後一個命令加上 `--dump-menu`。它會輸出 JSON 後結束，不開啟圖示。範例資料由原生選單測試檢查，避免快照格式與程式不同步。

Windows 通知區圖示可用 `python -m monitorbot menubar start` 啟動。使用方式見 [Windows 說明](docs/windows.md#隨時從工作列查看狀態)。
