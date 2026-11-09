# 驗證紀錄

查核日期為 2026-11-09，時區 Asia/Taipei。環境為 macOS 26.4、Python 3.9.6。

## 可重跑的驗證

```sh
python3 -m unittest discover -s tests -q
python3 scripts/verify_runtime.py --isolation
python3 scripts/verify_runtime.py --workflow
python3 -m monitorbot status
python3 -m monitorbot doctor
```

143 項測試在一般環境及實際 macOS sandbox 內均通過。隔離探測確認外部測試檔讀取、外部寫入與網路均被拒絕。父程序直接比對 SQLite，10 張原表資料保留且完整性通過。

Codex CLI 使用既有登入完成合成標記查核，已知變動 session 與 log 位置未發現持久標記。大型檔案採串流掃描。查核範圍不包含未知的第三方遙測位置。

真實 CLI 合成端到端測試建立了一項明確由使用者負責、須等待兩個月前置工作的任務。結果為 waiting、沒有虛構交付期限，且不符合閒置註銷條件。測試只使用臨時資料庫。

skill 已通過原版 quick_validate 與臨時資料的固定 CLI 行為查核。未將驗證所需 PyYAML 加入程式相依。

## 真實來源狀態

| 來源 | 查核結果 |
|---|---|
| Codex、Claude 本機對話 | 實際增量讀取與模型處理成功，工具輸出與 ephemeral 對話不列為已覆蓋 |
| Chrome | 多 profile 唯讀查詢。Profile 2 存在 exclusive lock，其餘 profile 紀錄保留，狀態為 partial，不繞過鎖或前進共用游標 |
| Word、Excel、PowerPoint | Open XML 解析與範圍行為測試通過，尚無近期實際文件可完成真實來源查核 |
| ChatGPT | 沒有直接對話來源，明確標記 unconfigured |
| Outlook、Teams | 連接介面與授權流程已實作；依使用者決定暫不設定 Microsoft 授權 |

Chrome 鎖定案例以真實唯讀 schema 查核定位，另用合成 exclusive lock 測試證明正常 profile 不被丟棄、解除鎖定後可以補抓。沒有關閉 Chrome、複製 History 或使用忽略鎖定的 SQLite 選項。

## 背景部署

Desktop 專案的 launchd 查核實際出現讀取權限錯誤。程式與資料已部署到使用者的 Application Support，沒有修改 macOS TCC 設定。

原專案與安裝版的 `.monitor` 指向同一份資料。CLI 在載入可變更模組前依 manifest 選擇安裝版，並處理中斷回復。已註冊登入啟動與 skill。

背景工作每分鐘檢查排程，活動分析每小時執行。實際運作狀態與最新來源檢查時間以 `status`、`doctor` 及本機資料庫為準。舊的 Desktop 啟動錯誤已保留封存，並卸載臨時診斷工作。

已由真正的 launchd 工作完成一輪真實來源分析，最後退出碼為 0。查核時共有 48 筆活動，未處理數為 0，近期沒有新的處理失敗紀錄。這是當次查核結果，不代表後續來源或登入永遠可用。

## 選單列驗證

選單列原生 app 已真實編譯、部署並設為登入自啟。使用者已確認可見圖示及待辦數字，且可以點開。自動 UI 工具無法取得這類無主視窗 app 的完整選單畫面，因此點開的視覺驗證採使用者實際回覆。

加入選單列後，完整單元測試為 154 項，全部通過。圖示獨立於背景收集，清單讀取不呼叫模型，編譯產物具有原生 app 識別與 LSUIElement 設定。

## 未驗證的能力

沒有將 Windows、未儲存 Office 編輯、瀏覽器網頁全文、簡報圖像理解或未授權 Microsoft 資料宣稱為已覆蓋。Claude 可替換執行後端的控制流程經測試，實際模型執行查核目前使用 Codex。

自動改善的測試核心完整性檢查及父程序資料比對能攔截已重現的偽成功，不能形式證明任意惡意 Python 安全。變更仍須通過獨立審查，失敗時不套用。

## Windows、Gerrit 與選單列更新

本次開發在 macOS 執行完整測試，共 170 項通過。Windows 真實排程與對話框尚未執行。

```sh
python3 -m unittest discover -s tests -q
python3 scripts/verify_windows.py
```

Windows 合成工具已驗證排程 XML 產生、中文資料路徑及中英混合待辦的隔離 CLI 寫入。輸出明列平台排程與對話框仍待檢查。在 Windows 執行 `python scripts/verify_windows.py --scheduler --dialogs`，工具會註冊、啟動、停止並移除臨時排程。詳細準備方式見 [Windows 操作](windows.md)。

Gerrit 合成測試涵蓋分頁、奈秒時間格式、索引去重與持久化後回查、排除項目不讀正文、分頁或內容失敗保留游標、來源更新標示、內容上限、HTTPS、XSSI 與憑證錯誤遮罩。額外回歸測試確認 Gerrit token 不傳入模型 CLI，以及舊編碼管線可正常回傳中文待辦 JSON。組織 Gerrit 帳號與權限尚未提供，驗證範圍見下方公開伺服器檢查。

mac 選單列經 Swift 編譯，並以合成快照執行真正的 NSMenu 建構與階層檢查。檢查包含五項第一層待辦、其餘待辦子選單、等待項目、逾期、來源狀態，以及空白與無效快照。`tests/test_menubar.py` 會自動編譯並執行這些檢查。`--snapshot FILE --dump-menu` 輸出同一個選單建構結果，可用 `--snapshot FILE` 啟動合成預覽。原生 UI 控制工具超時，畫面目視效果仍未驗證。臨時預覽程序已停止，未部署或重啟使用者現有服務。

此輪的決策與證據指標保存在 [.audit/platforms-gerrit.tsv](../.audit/platforms-gerrit.tsv)。

### 狀態快照與公開 Gerrit 補充檢查

後續完整測試為 172 項通過。新增整合測試從隔離資料庫啟動真正的 CLI，讀取背景排程狀態及最後收集檢查時間，再將 JSON 交給原生 NSMenu 建構。選單區分清單讀取、收集檢查與排程是否啟用；launchd 等候下一次 tick 時仍屬已啟用，不宣稱程序持續執行。

對官方公開伺服器 `https://gerrit-review.googlesource.com` 執行唯讀 API 檢查。change `644585` 的查詢、詳細內容與已發布留言讀取成功。另一筆已合併 change 經完整 `sources.collect` 收集成功，臨時資料庫確認重複索引不新增事件、正文未持久化。接著由已保存索引再次回查時，公開伺服器回覆 HTTP 429；程式回報 `SourceError`，本次完整回查未列為通過。合成索引回查測試仍通過。沒有使用私人帳號、模型或正式資料庫。

公開 API 成功不代表公司伺服器的 token、SSO、憑證鏈及組織權限已驗證。Windows 實機與 mac 畫面目視檢查仍未完成。

### 核准後的 Mac 部署

本輪完整測試為 173 項通過，另重新執行 Windows 合成工具。新增的合成預覽 fixture 位於 `tests/fixtures/menubar.json`，README 提供直接編譯與預覽命令。原生預覽 bundle 在 sandbox 內退出 134；同一執行檔移出 bundle 後可於 sandbox 執行，實際 bundle 的選單結構檢查則在 sandbox 外通過。這不等於畫面目視驗證。

使用者確認「現在部署並重新啟動」後，已停止原服務、部署新版並重新啟用登入啟動。部署前後逐表比較內容指紋，10 張資料表完全一致，SQLite 完整性檢查通過。18 個已安裝程式及相關檔案的 SHA-256 與工作目錄一致。原程式備份保留於安裝根目錄的 `install-backup/921f601103b64dc98eeef08709067c52`。

重新啟動後，原生選單列程序狀態為 running，實際 PID 與 `MonitorMenuBar` 程序核對成功；安裝版 `menubar snapshot` 亦成功，背景排程與登入啟動均啟用。背景排程最近退出碼為 0，等候下一次 tick 時顯示 not running 屬正常。此節取代前面「未部署或重啟」的舊交付狀態。

這些檢查在正式安裝的使用者權限下執行；sandbox 無權以寫入模式開啟正式 SQLite，不能用該限制推斷安裝版故障。僅輸出狀態與雜湊，沒有將來源正文另存為驗證產物。詳細部署證據存於本機忽略的 `.build/deploy-verified.json`，程式與預覽指紋存於 `.build/release-check.json`。

Mac 選單的實際可見性已請使用者確認。Windows 排程與對話框實機，以及公司 Gerrit 帳號權限仍未驗證。
