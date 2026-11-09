# 在 Windows 啟動與驗證 Monitor bot

使用 Windows 的一般使用者帳號登入桌面。在固定位置保留本專案，排程會從該位置載入程式。Windows 使用工作排程器，不需要執行 macOS 的 `deploy`。

## 準備 Python 與模型 CLI

安裝 Python 3.9 以上版本，保留安裝程式的 Tcl/Tk 選項。使用同一份 Python 安裝時區資料。

```powershell
python -m pip install tzdata
python -m tkinter
```

第二個命令會開啟 Tk 測試視窗。關閉視窗後繼續。

安裝原生 Windows 模型 CLI 並完成帳號登入。將執行檔放進使用者的 `PATH`，或在 `agents.default.executable` 指定 `.exe` 的完整路徑。若安裝方式只提供 npm 的 `.cmd` 包裝器，改用原生執行檔。此版不處理批次檔包裝器。

在專案目錄初始化並檢查環境。

```powershell
python -m monitorbot init
python -m monitorbot doctor
python -m monitorbot agent-check --backend codex
```

`agent-check` 會使用模型 CLI 額度。來源正文只有在該後端通過合成內容保存檢查後，才會交給模型。

## 先執行隔離測試

先跑不註冊排程的測試。

```powershell
python scripts/verify_windows.py
```

工具會產生 Windows 排程 XML，在暫存資料目錄建立一項合成待辦，並確認寫入成功。這個模式可在 macOS 執行，輸出會明列尚未驗證的 Windows 排程與對話框。

在 Windows 執行完整平台測試。

```powershell
python scripts/verify_windows.py --scheduler --dialogs
```

工具會註冊一個獨立的使用者排程，啟動背景程序，等候資料庫出現輪詢紀錄，驗證重複啟動與停止行為，最後移除測試排程。所有來源、每日彈窗與自動改善都停用。測試不讀真實來源，不呼叫模型，不修改正式安裝。

`--dialogs` 另外開啟合成確認視窗。選取確認項目並按確認。若只要自動驗證排程，省略 `--dialogs`。

若測試被強制終止，可能留下名稱為 `local.monitorbot.*` 的測試排程。在工作排程器檢查描述中的暫存資料路徑，只移除對應的測試排程。

## 啟動正式背景收集

先設定來源範圍並手動輪詢，再準備排程。

```powershell
python -m monitorbot poll
python -m monitorbot install
python -m monitorbot start --login
python -m monitorbot status
```

`install` 只在資料目錄寫入 `windows-task.xml`。`start` 才註冊並啟動排程。`--login` 加上目前使用者的登入觸發器。排程以目前使用者的權限執行，每分鐘檢查既有排程，不保存 Windows 密碼。使用者登出後不收集資料。

`status` 的 `registered` 表示工作排程器中存在本安裝的排程，`loaded` 表示程序正在執行，`login_enabled` 表示有登入觸發器。啟動程序需要時間，可稍後重查 `status`。`last_exit_code` 是工作排程器回報的結果，正在執行的工作可能顯示 `267009`。

單獨 `stop` 結束目前程序，保留登入啟動。`stop --login` 結束程序並移除本安裝的排程，不刪除資料。

```powershell
python -m monitorbot stop
python -m monitorbot stop --login
```

## 驗證每日確認與來源範圍

手動開啟每日確認。

```powershell
python -m monitorbot review
```

Windows 使用 Tk 對話框回答問題。選取開啟完整清單時，使用系統預設應用程式開啟 `daily-todo.md`。本版沒有 Windows 系統匣圖示。

Chrome 會從 `%LOCALAPPDATA%\Google\Chrome\User Data` 尋找 profile。Office 文件請設定 `sources.word.paths`、`sources.excel.paths` 或 `sources.powerpoint.paths`。Windows 尚未自動讀取 Office 最近使用清單。其餘來源設定見 [README](../README.md)。

若組織原則禁止 PowerShell COM 或使用者建立排程，先改用前景執行，並交由管理員處理權限。

```powershell
python -m monitorbot run --loop
```

目前開發環境未執行 Windows 工作排程器與 Windows 對話框。請以本頁的平台測試結果判定實機可用性。
