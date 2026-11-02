# Changelog

本專案的重要變更會記錄在此文件。格式參考 [Keep a Changelog](https://keepachangelog.com/zh-TW/1.1.0/)，版本號採用 [Semantic Versioning](https://semver.org/lang/zh-TW/)。

## [0.1.0] - 2026-11-02

### 新增

- 本機 Web 介面（`taiwan-stock web`），使用 SQLite 儲存資料
- 我的持股：市值、損益、報酬率、成本殖利率，以及含費用回本價、扣息回本價與賣出試算
- 商品查詢：同步 TWSE／TPEx 股票與 ETF 名單、上下市狀態及 ETF 官方基本資料
- 交易紀錄：依商品與日期自動試算費用，支援篩選、CSV 匯出與預覽後匯入
- 股票異動：自動同步分割與股票股利
- 領息月曆：依持股試算配息，可依證券帳戶或領息銀行篩選
- 背景同步任務與 `taiwan-stock sync` CLI 指令
- 本機資料庫備份與還原
- 初版公開發布
