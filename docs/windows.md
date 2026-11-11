# 在 Windows 啟動與驗證 Monitor bot

使用 Windows 的一般使用者帳號登入桌面。在固定位置保留本專案，排程會從該位置載入程式。Windows 使用工作排程器，不需要執行 macOS 的 `deploy`。

## 準備 Python 與模型 CLI

安裝 Python 3.9 以上版本，保留安裝程式的 Tcl/Tk 選項。使用同一份 Python 安裝時區資料。

```powershell
python -m pip install tzdata
python scripts/verify_windows_ui.py
```

第二個命令會開啟合成 Tk 視窗並自動操作，完成後自動關閉。請在專案目錄執行。程式會處理內附 Tcl/Tk 在中文 Python 安裝路徑下的啟動問題。

安裝原生 Windows 模型 CLI 並完成帳號登入。將執行檔放進使用者的 `PATH`，或在 `agents.default.executable` 指定 `.exe` 的完整路徑。若安裝方式只提供 npm 的 `.cmd` 包裝器，改用原生執行檔。此版不處理批次檔包裝器。

在專案目錄初始化並檢查環境。

```powershell
python -m monitorbot init
python -m monitorbot doctor
python -m monitorbot agent-check --backend codex
```

`agent-check` 會使用模型 CLI 額度。來源正文只有在該後端通過合成內容保存檢查後，才會交給模型。

Microsoft 登入檔與權杖在 Windows 使用目前帳號專用的 ACL。程式會在寫入前設定權限，讀取前檢查權限。若既有權杖檔開放給其他帳號讀取，重新執行 Microsoft 登入流程。

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

自動驗證實際視窗操作，不需要手動選取。

```powershell
python scripts/verify_windows_ui.py
python -m unittest discover -s tests
```

UI 驗證會開啟合成視窗，操作選項、中文輸入、確認與取消，完成後自動關閉。請在已登入的 Windows 桌面執行。單元測試會明列需要 macOS 或符號連結權限而略過的項目。

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

Windows 使用 Tk 對話框回答問題。長問題與選項可以捲動，底部保留確認與稍後按鈕。按 Enter 確認，按 Esc 稍後處理。自行填寫的視窗使用相同版面。選取開啟完整清單時，使用系統預設應用程式開啟 `daily-todo.md`。右下角通知區提供常駐狀態圖示，使用方式見下節。

Chrome 會從 `%LOCALAPPDATA%\Google\Chrome\User Data` 尋找 profile。Office 文件請設定 `sources.word.paths`、`sources.excel.paths` 或 `sources.powerpoint.paths`。Windows 尚未自動讀取 Office 最近使用清單。其餘來源設定見 [README](../README.md)。

若組織原則禁止 PowerShell COM 或使用者建立排程，先改用前景執行，並交由管理員處理權限。

```powershell
python -m monitorbot run --loop
```

平台測試只使用暫存合成資料，不驗證真實 Microsoft 授權、組織網路或模型帳號。設定帳號後，另執行 `agent-check` 與 `poll`，確認該帳號的登入與來源狀態。

## 隨時從工作列查看狀態

在專案目錄啟動通知區圖示。

```powershell
python -m monitorbot menubar start
python -m monitorbot menubar status
```

工作列右下角會出現 Monitor 圖示。若 Windows 將圖示收進隱藏區，點選向上的箭頭並將圖示拖至通知區。左鍵或右鍵點擊圖示可查看背景收集、最後收集時間、待辦、待確認問題及來源狀態。選單每分鐘更新，開啟時也會更新。來源讀取失敗會標示錯誤並保留上次資料。

選單的「開啟每日清單」從目前資料庫產生清單，不呼叫模型。「每日確認」使用既有確認流程。「新增待辦」可直接輸入中文工作項目。

`start` 啟動背景收集時也會啟動圖示。單獨 `menubar start` 只啟動狀態圖示，尚未啟動收集時會顯示「背景收集未啟用」。這可用來查看既有資料。

退出圖示不會停止背景收集。停止背景收集後，圖示仍可查看停止狀態。只要關閉圖示時執行下列命令。

```powershell
python -m monitorbot menubar stop
```

圖示目前只在本次登入期間執行，不新增登入啟動項。`start --login` 保留原本的背景收集登入排程，圖示在下次登入後可用 `menubar start` 開啟。`menubar status` 的 `login_enabled` 因此為 `false`。

圖示使用 Windows 內建 PowerShell 與 Windows Forms，不需要安裝其他 Python 套件。啟動錯誤記錄在資料目錄的 `tray-errors.log`。

在已登入的 Windows 桌面執行實際通知區操作驗證。

```powershell
python scripts/verify_windows_tray.py
```

驗證以獨立合成資料顯示真正的選單，測試更新失敗、更新後的待辦數、重複啟動和退出。畫面及結果儲存在 `.audit/windows-tray-ui.json.png` 和 `.audit/windows-tray-ui.json`。
