# 來源設定與涵蓋範圍

來源設定在本機 `config.json` 的 `sources`。每種來源有自己的 `enabled`、`include`、`exclude`。來源未接通會顯示 `unconfigured`，讀取或分頁失敗顯示 `error`，並保留最後成功位置。

後續輪詢重疊最近兩分鐘以減少短暫寫入延遲造成的遺漏，由活動識別去重。首次輪詢不因此匯入啟用前的資料。這不能保證補到延遲超過兩分鐘才發佈的活動，若實際來源有此行為須改用更完整的增量機制。

## Chrome

預設讀取本機 Chrome 的 Default 與 Profile 目錄，可用 `sources.chrome.profiles` 指定 profile 目錄清單。程式以唯讀 SQLite 讀取 `History`，不建立歷史副本。

紀錄包含瀏覽時間、標題、網址與 profile。無痕活動不在 Chrome History 中。網址移除 fragment、帳密及常見驗證參數。瀏覽歷史不包含網頁全文，也不能證明目前仍在看某個頁面。需要頁面內容時須另外接上已授權的瀏覽器讀取入口，不能把歷史標題當成網頁內容。

## Word、Excel 與 PowerPoint

Mac 預設透過 Spotlight 最近使用時間取得 Office 文件活動。每種來源可用 `paths` 指定觀察檔案或目錄，指定目錄才會搜尋該目錄下的對應文件。`recent: false` 可關閉 Spotlight 查詢。

```json
{
  "sources": {
    "word": {"enabled": true, "paths": ["/path/to/work"], "recent": true},
    "excel": {"enabled": true, "paths": ["/path/to/model.xlsx"]},
    "powerpoint": {"enabled": true, "paths": ["/path/to/slides.pptx"]}
  }
}
```

內文只在需要時從原檔讀取。支援 `.docx` 文字、`.pptx` 投影片與備註、`.xlsx` 儲存格文字、公式及已儲存的計算結果。不執行巨集，不重新計算公式，不建立文件副本。

內文預設讀取上限為 100,000 字元，可用 `content_max_chars` 調整。超過上限會回報尚未完整讀取，不以截斷片段宣告工作完成。Office XML 另有 32 MiB 解壓大小上限。大型內容需要後續分段讀取流程，或使用者調整上限。

此來源不捕捉未儲存的編輯、每次滑鼠操作、圖片中的文字或瀏覽器 Office 網頁內文。Spotlight 未提供最近使用資料時，不能推定沒有活動。未找到最近使用文件且未指定路徑時顯示 `unconfigured`。

## Codex 與 Claude 對話

預設讀取 `~/.codex/sessions` 與 `~/.claude/projects` 的既存 JSONL。可用各來源的 `roots` 改為指定目錄。

只擷取使用者與 assistant 的文字訊息，不收工具輸出、推理內容或憑證。索引保存檔案、行號、內容摘要雜湊與角色，原文只在記憶體流動。回查時核對雜湊，檔案行內容改變會回報無法驗證。ephemeral 對話沒有既存紀錄，因此無法由此來源還原。

ChatGPT 桌面或網頁對話尚未有可讀取的授權入口時顯示 `unconfigured`。Chrome 的 ChatGPT 網址紀錄不等於已擷取問題與回答。

## Teams 與 Outlook

來源使用 Microsoft Graph delegated OAuth，不借用瀏覽器 cookie，不擷取既有應用程式的 token。模型沿用 CLI 登入，不代表 Microsoft 來源已授權。

需要自己的 Microsoft Entra public-client app `client_id`，並允許 device-code 登入。`client_id` 不是 API Key 或 client secret。組織可能要求管理員核准，或禁止此登入流程。

```json
{
  "microsoft": {
    "client_id": "YOUR-REGISTERED-PUBLIC-CLIENT-ID",
    "tenant": "organizations",
    "max_pages": 100
  }
}
```

登入由明確的使用者指令開始。`start_microsoft_login(config)` 回傳登入網址、user code、device code、到期時間與輪詢間隔。呼叫端在使用者登入後，以 `finish_microsoft_login(config, device_code)` 輪詢一次，遵守原始間隔與到期時間。`authorization_pending` 時仍未登入成功，不能視為來源已接通。

必要 token 存在本工具資料目錄的 `microsoft-auth.json`，權限為 `0600`。不保存原始 API 回應、郵件或聊天副本。存取 token 到期時使用 refresh token，失效時要求重新登入。

Outlook 預設讀取登入者信箱的更新訊息索引，包含寄件。讀取內文需要 delegated `Mail.Read`。索引包含來源訊息 ID、討論串、資料夾、寄件者、收件者與回查連結。

Teams 每輪重新取得聊天清單與加入的團隊，再讀聊天室訊息、頻道討論與回覆。可設定 `chat_ids`、`team_ids`、`channel_ids` 限定範圍，空清單代表不納入該類別。聊天讀取需要 `Chat.Read`，頻道內容需要 `ChannelMessage.Read.All`，列出團隊與頻道另外需要 `Team.ReadBasic.All`、`Channel.ReadBasic.All`。Teams delegated 讀取不支援個人 Microsoft 帳號。

Teams 訊息列表本身包含 body。來源規則可阻止內容傳入 agent 與持久儲存，但伺服器回傳的訊息仍可能含被排除內容。要避免向伺服器請求整個聊天室或團隊，使用 `chat_ids`、`team_ids` 與 `channel_ids` 限定來源。分頁必須全部完成才推進游標；碰到權限、節流或分頁上限會回報錯誤，不宣稱完整同步。

官方文件：

- [Device-code OAuth 流程](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code)
- [Outlook 訊息列表與讀取權限](https://learn.microsoft.com/en-us/graph/api/user-list-messages?view=graph-rest-1.0)
- [Teams 聊天訊息與時間篩選](https://learn.microsoft.com/en-us/graph/api/chat-list-messages?view=graph-rest-1.0)
- [Teams 頻道訊息與回覆](https://learn.microsoft.com/en-us/graph/api/channel-list-messages?view=graph-rest-1.0)

## 目前尚未實測的能力

測試使用合成 Chrome、Office、對話與 Graph 回應，不會把測試事件寫入正式資料庫。真實帳號登入、租戶權限、Microsoft 分頁量、Spotlight 完整度與 Chromium 資料庫鎖定須在實際環境確認。
