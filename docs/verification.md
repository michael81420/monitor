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
