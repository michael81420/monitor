# 實作進度

- [x] Ground：空專案，已確認需求與 Mac 環境。
- [x] Sketch：兩個獨立候選比較集中 Store 與事件投影。
- [x] Agree：採目前狀態加追加歷史，既有設計已由使用者確認。
- [x] Implement：資料與規則、來源介面、agent、背景排程與每日介面。
- [x] Scrap：保留集中 Store；因 Desktop 背景權限限制，執行副本與單一資料庫部署至 Application Support。
- [x] Verify：143 項測試及真實 OS 隔離通過，CLI 合成端到端通過，真實來源與背景狀態另記於 verification.md。

架構比較依序檢查資料寫入的一致性、原文是否落盤、歷史可追溯、模組介面大小及實作成本。

本環境只有 GPT 系列 agent，架構候選使用繼承模型的獨立上下文。無可用的跨模型家族 judge，由主 agent 對照需求選擇，之後另做獨立實作審查。

Microsoft 授權由使用者決定暫不設定。ChatGPT 直接來源尚未接通，Office 尚無實際使用文件可查核，Windows 尚未驗證。這些限制不作為已覆蓋來源。
